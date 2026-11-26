"""Private, persistent .torrent library for imports managed independently of qBittorrent.

New WebUI imports keep their original .torrent filename in /data/torrents/.
Legacy infohash-named files remain readable and are never renamed automatically.
Files are never sent to qBittorrent. Trackers and their passkeys are never returned in
API responses. No downloaded payload is needed for metadata monitoring.
"""
from __future__ import annotations
import os
import re
import logging
import threading
from pathlib import Path
from core import decode_bencode, domain, domain_matches, rule_preference
from torrent_import import ImportErrorValue, MAX_TORRENT_BYTES, inspect_torrent, validate_torrent_filename

HASH = re.compile(r'^[0-9a-f]{40}$')
LOG = logging.getLogger('joal-smart')


def _mode(name, default):
    raw = os.getenv(name, default)
    try:
        mode = int(raw, 8)
    except (TypeError, ValueError) as exc:
        raise ImportErrorValue(f'{name} doit être un mode octal, ex. 0666') from exc
    if not 0 <= mode <= 0o777:
        raise ImportErrorValue(f'{name} hors limites (0000..0777)')
    return mode


def _owner(name, default):
    try:
        result = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ImportErrorValue(f'{name} doit être numérique') from exc
    if not 0 <= result <= 65535:
        raise ImportErrorValue(f'{name} doit être compris entre 0 et 65535')
    return result



def announce_urls(payload):
    root, end = decode_bencode(payload)
    if end != len(payload) or not isinstance(root, dict):
        raise ImportErrorValue('Métadonnées .torrent invalides')
    raw = [root.get(b'announce')]
    tiers = root.get(b'announce-list', [])
    if isinstance(tiers, list):
        for tier in tiers:
            if isinstance(tier, list):
                raw.extend(tier)
            elif isinstance(tier, bytes):
                raw.append(tier)
    result = []
    for value in raw:
        if not isinstance(value, bytes):
            continue
        try:
            url = value.decode('utf-8')
        except UnicodeError:
            continue
        if domain(url) and url not in result:
            result.append(url)
    return result[:32]


class SoloLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._hashes = {}

    def _adjust_permissions(self, path, mode):
        """Best-effort Unraid nobody:users permissions, on .torrent files only.

        Unraid user shares can refuse chown/chmod even when the process can write.
        Such failures must not invalidate a correctly saved torrent.
        """
        try:
            os.chmod(path, mode)
        except (PermissionError, OSError) as exc:
            LOG.warning('chmod refusé sur la bibliothèque torrents (%s): %s', type(exc).__name__, path.name)
        try:
            uid = _owner('TORRENT_UID', 99)
            gid = _owner('TORRENT_GID', 100)
            if (path.stat().st_uid, path.stat().st_gid) != (uid, gid):
                os.chown(path, uid, gid)
        except (PermissionError, OSError) as exc:
            LOG.warning('chown nobody:users refusé sur la bibliothèque torrents (%s): %s', type(exc).__name__, path.name)

    def _ensure(self):
        if self.root.is_symlink():
            raise ImportErrorValue('Dossier de torrents symbolique interdit')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o770)
        if self.root.is_symlink():
            raise ImportErrorValue('Dossier de torrents symbolique interdit')
        self._adjust_permissions(self.root, _mode('TORRENT_DIR_MODE', '0770'))

    def _torrent_paths(self):
        """Only regular .torrent files directly within the managed library."""
        if not self.root.is_dir() or self.root.is_symlink():
            return []
        result = []
        for path in sorted(self.root.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                validate_torrent_filename(path.name)
            except ImportErrorValue:
                continue
            result.append(path)
        if len(self._hashes) > len(result):
            present = set(result)
            self._hashes = {p: entry for p, entry in self._hashes.items() if p in present}
        return result

    def _file_hash(self, path):
        stat = path.stat()
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = self._hashes.get(path)
        if cached and cached[0] == signature:
            return cached[1]
        info, _ = self._read_torrent(path, [])
        self._hashes[path] = (signature, info['hash'].lower())
        return self._hashes[path][1]

    def _read_torrent(self, path, rules):
        """Validate payload; retain the old filename/hash integrity check."""
        if path.stat().st_size > MAX_TORRENT_BYTES:
            raise ImportErrorValue('Métadonnées .torrent trop volumineuses')
        blob = path.read_bytes()
        info = inspect_torrent(blob, rules, require_match=False)
        if HASH.fullmatch(path.stem) and info['hash'].lower() != path.stem:
            raise ImportErrorValue('Infohash ne correspond pas au nom du fichier historique')
        return info, blob

    def normalize_permissions(self):
        """Apply user-requested Unraid permissions to imported and legacy files."""
        with self._lock:
            self._ensure()
            for path in self._torrent_paths():
                self._adjust_permissions(path, _mode('TORRENT_FILE_MODE', '0660'))

    def _unique_name(self, filename, infohash):
        """Preserve the original basename unless a different torrent owns it."""
        original_names = {p.name.casefold() for p in self._torrent_paths()}
        if filename.casefold() not in original_names and not (self.root / filename).exists():
            return filename
        # Two different torrents can share a basename. Do not overwrite either.
        stem, extension = filename[:-8], filename[-8:]
        for width in (10, 20, 40):
            suffix = ' [' + infohash[:width] + ']'
            prefix = stem
            while prefix and len((prefix + suffix + extension).encode('utf-8')) > 255:
                prefix = prefix[:-1]
            candidate = prefix + suffix + extension
            if candidate.casefold() not in original_names and not (self.root / candidate).exists():
                return candidate
        raise ImportErrorValue('Conflit de noms de fichiers .torrent')

    def add(self, payload, rules, filename=None):
        """Import from WebUI with original filename; preserve infohash identity."""
        info = inspect_torrent(payload, rules, require_match=False)
        h = info['hash'].lower()
        if not HASH.fullmatch(h):
            raise ImportErrorValue('Torrents BitTorrent v2 uniquement non pris en charge par JOAL')
        # Internal legacy callers without a filename still use hash storage.
        name = validate_torrent_filename(filename if filename is not None else h + '.torrent')
        with self._lock:
            self._ensure()
            for existing in self._torrent_paths():
                try:
                    existing_hash = self._file_hash(existing)
                except (OSError, ValueError, ImportErrorValue, RecursionError):
                    continue
                if existing_hash == h:
                    raise ImportErrorValue('Ce torrent est déjà dans la bibliothèque JOAL uniquement')
            stored_name = self._unique_name(name, h)
            target = self.root / stored_name
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError as exc:
                raise ImportErrorValue('Conflit de nom lors de l’enregistrement du torrent') from exc
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._adjust_permissions(target, _mode('TORRENT_FILE_MODE', '0660'))
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        info['fileName'] = stored_name
        return info

    def list(self, rules):
        """Read original and hash-named files; verify each torrent by its infohash."""
        result = []
        with self._lock:
            for path in self._torrent_paths():
                try:
                    info, blob = self._read_torrent(path, rules)
                    info['urls'] = announce_urls(blob)
                    info['payload'] = blob
                    info['fileName'] = path.name
                    info['addedOn'] = path.stat().st_mtime
                    result.append(info)
                except (OSError, ValueError, ImportErrorValue, RecursionError):
                    continue
        return result

    def delete_many(self, infohashes):
        """Delete several .torrent files in a single pass over the library (v1.30.2).
        Returns the set of hashes actually removed; unknown hashes are ignored."""
        wanted = {str(h).lower() for h in infohashes}
        if not wanted or not all(HASH.fullmatch(h) for h in wanted):
            raise ImportErrorValue('Infohash invalide')
        removed = set()
        with self._lock:
            for path in self._torrent_paths():
                try:
                    existing_hash = self._file_hash(path)
                except (OSError, ValueError, ImportErrorValue, RecursionError):
                    continue
                if existing_hash in wanted and existing_hash not in removed:
                    path.unlink()
                    self._hashes.pop(path, None)
                    removed.add(existing_hash)
        return removed

    def delete(self, infohash):
        h = str(infohash).lower()
        if not HASH.fullmatch(h):
            raise ImportErrorValue('Infohash invalide')
        with self._lock:
            for path in self._torrent_paths():
                try:
                    existing_hash = self._file_hash(path)
                except (OSError, ValueError, ImportErrorValue, RecursionError):
                    continue
                if existing_hash == h:
                    path.unlink()
                    self._hashes.pop(path, None)
                    return
        raise FileNotFoundError(h)


def matching_rules(info, rules, manual_rule=None):
    """Find eligible rule/domain pairs ordered by their effective threshold.

    A tracker can occur in several rules; a torrent is assigned to one rule
    only. An infohash override is respected when ranking the rules.
    """
    matches = []
    for rule in rules:
        if not rule['enabled']:
            continue
        if manual_rule:
            if rule['id'] != manual_rule:
                continue
            # The owner explicitly chose this policy for this infohash.
            # Use only trackers carried by that .torrent, never arbitrary URLs.
            urls = list(dict.fromkeys(u for u in info['urls'] if domain(u)))
        else:
            urls = list(dict.fromkeys(u for u in info['urls'] if any(
                domain_matches(domain(u), pattern) for pattern in rule['trackerDomains'])))
        if urls:
            matches.append((rule, urls))
    return sorted(matches, key=lambda pair: rule_preference(pair[0], info['hash']))


def applicable_rule(info, rules, manual_rule=None):
    """Return explicit torrent rule, otherwise least restrictive matching one."""
    pairs = matching_rules(info, rules, manual_rule)
    return pairs[0] if pairs else (None, [])


def confirmed_leechers(observation, min_count):
    """Check leecher threshold; zero means no leecher check at all.

    With a positive threshold, only fresh, validated tracker observations are
    allowed. A threshold of zero is an explicit policy opt-out, NOT a fabricated
    tracker observation and never changes the displayed unknown count.
    """
    if min_count == 0:
        return True
    count = observation.get('leechers')
    if (type(count) is not int or count < min_count or not observation.get('sampledAt')
            or observation.get('status') not in ('récent', 'partiel')
            or observation.get('successCount', 0) <= 0):
        return False
    return True
