"""Bounded, verified ZIP backups for the persistent /data configuration.

Deliberately excludes runtime JVM files, transient logs, cache and Docker .env.
Restore is scheduled by an authenticated API and applied BEFORE any background
threads start, with a local pre-restore checkpoint and rollback on failures.
"""
from __future__ import annotations
import datetime as dt
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from core import validate
from interface_design import DESIGNS, RETIRED
from torrent_import import MAX_TORRENT_BYTES, ImportErrorValue, validate_torrent_filename

PERSISTENT_JSON = frozenset({
    'policy.json', 'tracker-registry.json', 'cleanup-observations.json',
    'handoff-state.json', 'diagnostics.json', 'notifications.json', 'simulation-modes.json',
    'interface.json'
})
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_TOTAL = 60 * 1024 * 1024
MAX_ENTRY = 12 * 1024 * 1024
MAX_ITEMS = 1200
PENDING_NAME = '.restore-pending.zip'
CLIENT = re.compile(r'^clients/[a-zA-Z0-9_.-]{1,100}\.client$')
TORRENT_HASH = re.compile(r'^[0-9a-f]{40}$')


def permitted(name):
    if name in PERSISTENT_JSON or CLIENT.fullmatch(name):
        return True
    if not isinstance(name, str) or not name.startswith('torrents/'):
        return False
    try:
        return validate_torrent_filename(name[len('torrents/'):]) == name[len('torrents/'):]
    except ImportErrorValue:
        return False


def list_files(root):
    root = Path(root)
    found = []
    for filename in sorted(PERSISTENT_JSON):
        p = root / filename
        if p.is_symlink():
            raise ValueError('Lien symbolique interdit : ' + filename)
        if p.is_file():
            found.append((filename, p))
    client_dir = root / 'clients'
    if client_dir.is_symlink():
        raise ValueError('Lien symbolique interdit : clients')
    if client_dir.is_dir():
        for path in sorted(client_dir.iterdir()):
            name = 'clients/' + path.name
            if path.is_symlink():
                raise ValueError('Lien symbolique interdit : ' + name)
            if path.is_file() and permitted(name):
                found.append((name, path))
    torrent_dir = root / 'torrents'
    if torrent_dir.is_symlink():
        raise ValueError('Lien symbolique interdit : torrents')
    if torrent_dir.is_dir():
        for path in sorted(torrent_dir.iterdir()):
            name = 'torrents/' + path.name
            if path.is_symlink():
                raise ValueError('Lien symbolique interdit : ' + name)
            if path.is_file() and permitted(name):
                found.append((name, path))
    return found


def build_backup(root):
    files = list_files(root)
    if len(files) > MAX_ITEMS:
        raise ValueError('Trop de fichiers à sauvegarder')
    total = 0
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name, path in files:
            size = path.stat().st_size
            if size > (MAX_TORRENT_BYTES if name.startswith('torrents/') else MAX_ENTRY) or total + size > MAX_TOTAL:
                raise ValueError('La taille des données persistantes dépasse la limite de sauvegarde')
            total += size
            zf.write(path, arcname=name)
    blob = output.getvalue()
    if len(blob) > MAX_ARCHIVE:
        raise ValueError('Archive trop volumineuse')
    return blob


def inspect_backup(blob):
    if not isinstance(blob, bytes) or not 0 < len(blob) <= MAX_ARCHIVE:
        raise ValueError('ZIP absent ou trop volumineux (64 Mio maximum)')
    try:
        result = {}
        total = 0
        seen_hashes = set()
        with zipfile.ZipFile(io.BytesIO(blob), 'r') as zf:
            members = zf.infolist()
            if len(members) > MAX_ITEMS:
                raise ValueError('Trop de fichiers')
            for member in members:
                name = member.filename
                if member.is_dir() or not permitted(name) or name in result:
                    raise ValueError('Chemin interdit ou doublon dans la sauvegarde')
                # No symbolic links or unusual file types.
                mode = (member.external_attr >> 16) & 0xffff
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError('Type de fichier interdit')
                if member.file_size > (MAX_TORRENT_BYTES if name.startswith('torrents/') else MAX_ENTRY) or total + member.file_size > MAX_TOTAL:
                    raise ValueError('Contenu ZIP trop volumineux')
                total += member.file_size
                data = zf.read(member)
                if len(data) != member.file_size:
                    raise ValueError('Fichier ZIP incomplet')
                if name.startswith('torrents/'):
                    from torrent_import import inspect_torrent
                    try:
                        meta = inspect_torrent(data, [], require_match=False)
                        stored_name = PurePosixPath(name).name
                        # Keep strict validation for existing hash-named torrents.
                        if TORRENT_HASH.fullmatch(stored_name[:-8]) and meta['hash'] != stored_name[:-8]:
                            raise ValueError('Infohash invalide dans la sauvegarde')
                    except (ValueError, IndexError, RecursionError) as exc:
                        raise ValueError('Fichier .torrent invalide dans la sauvegarde') from exc
                    if meta['hash'] in seen_hashes:
                        raise ValueError('Torrent dupliqué dans la sauvegarde')
                    seen_hashes.add(meta['hash'])
                if name.endswith('.json'):
                    parsed = json.loads(data.decode('utf-8'))
                    if not isinstance(parsed, dict):
                        raise ValueError('Fichier JSON non valide : ' + name)
                    if name == 'simulation-modes.json' and not all(
                            TORRENT_HASH.fullmatch(h) and mode in ('forced', 'forced-zero', 'stop') for h, mode in parsed.items()):
                        raise ValueError('Mode de simulation invalide')
                    if name == 'interface.json' and (set(parsed) != {'design'} or parsed['design'] not in {*DESIGNS, *RETIRED}):
                        raise ValueError('Design d’interface invalide dans la sauvegarde')
                    if name == 'policy.json':
                        validate(parsed)
                result[name] = data
        if 'policy.json' not in result:
            raise ValueError('policy.json est obligatoire dans la sauvegarde')
        return result
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, json.JSONDecodeError, OSError) as exc:
        raise ValueError('Archive ZIP invalide ou corrompue') from exc


def schedule_restore(root, blob):
    files = inspect_backup(blob)
    root = Path(root)
    dest = root / PENDING_NAME
    if dest.exists():
        raise ValueError('Une restauration est déjà en attente : redémarrer le conteneur')
    tmp = root / (PENDING_NAME + '.tmp')
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(files)


def apply_pending_restore(root):
    root = Path(root)
    pending = root / PENDING_NAME
    if not pending.is_file():
        return False
    restored = inspect_backup(pending.read_bytes())
    # On restore, never launch JOAL nor delete files immediately after restart.
    policy = json.loads(restored['policy.json'].decode('utf-8'))
    policy['dryRun'] = True
    policy.setdefault('handoff', {})['enabled'] = False
    policy.setdefault('cleanup', {})['deleteData'] = False
    policy['cleanup']['enabled'] = False
    validate(policy)
    restored['policy.json'] = (json.dumps(policy, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
    # The old handoff ownership state belongs to the previous live environment.
    # Do not resume torrents based on a restored session snapshot.
    if 'handoff-state.json' in restored:
        ledger = json.loads(restored['handoff-state.json'].decode('utf-8'))
        ledger['entries'] = {}
        restored['handoff-state.json'] = json.dumps(ledger).encode('utf-8')

    backups = root / 'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')
    checkpoint = backups / ('pre-restore-' + stamp + '.zip')
    counter = 0
    while checkpoint.exists():
        counter += 1
        checkpoint = backups / ('pre-restore-' + stamp + f'-{counter}.zip')
    previous_backup = build_backup(root)
    fd = os.open(checkpoint, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(previous_backup)
        stream.flush()
        os.fsync(stream.fileno())
    originals = {}
    updated = []
    try:
        for name, content in restored.items():
            target = root / name
            if target.is_symlink() or target.parent.is_symlink():
                raise ValueError('Lien symbolique présent dans le dossier de restauration')
            target.parent.mkdir(parents=True, exist_ok=True)
            originals[name] = target.read_bytes() if target.exists() else None
            temp = target.with_name(target.name + '.restore-tmp')
            with open(temp, 'xb') as f:
                os.chmod(temp, 0o600)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, target)
            updated.append(name)
    except BaseException:
        for name in reversed(updated):
            target = root / name
            original = originals[name]
            if original is None:
                target.unlink(missing_ok=True)
            else:
                temp = target.with_name(target.name + '.rollback-tmp')
                temp.write_bytes(original)
                os.chmod(temp, 0o600)
                os.replace(temp, target)
        raise
    pending.unlink()
    # Keep the latest three checkpoints; no deletion of other files in /data.
    for old in sorted(backups.glob('pre-restore-*.zip'))[:-3]:
        old.unlink(missing_ok=True)
    return True
