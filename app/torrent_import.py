"""Validation locale des fichiers .torrent et de l'import HTTP multipart.

Aucune annonce réseau; seuls les domaines de tracker sont renvoyés au navigateur.
"""
from email import policy
from email.parser import BytesParser
import hashlib

from core import decode_bencode, domain, domain_matches, get_info_slice

MAX_TORRENT_MIB = 50
MAX_TORRENT_BYTES = MAX_TORRENT_MIB * 1024 * 1024
MAX_MULTIPART_BYTES = MAX_TORRENT_BYTES + 16384
MAX_BATCH_FILES = 30
MAX_BATCH_BYTES = 64 * 1024 * 1024
MAX_BATCH_MULTIPART_BYTES = MAX_BATCH_BYTES + 65536


class ImportErrorValue(ValueError):
    pass



def validate_torrent_filename(filename: str) -> str:
    """Keep the uploaded basename unchanged; reject unsafe filesystem names.

    This also validates names received from ZIP backup archives. Do not
    normalize Unicode or silently rewrite a user-supplied filename.
    """
    if not isinstance(filename, str) or not filename.lower().endswith('.torrent'):
        raise ImportErrorValue('Extension .torrent requise')
    if len(filename) <= len('.torrent') or filename.startswith('.') or filename.endswith((' ', '.')):
        raise ImportErrorValue('Nom de fichier .torrent invalide')
    if '/' in filename or '\\' in filename or any(ord(c) < 32 or 127 <= ord(c) <= 159
                                                 or c in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069'
                                                 for c in filename):
        raise ImportErrorValue('Chemin ou caractères de contrôle interdits dans le nom')
    if len(filename.encode('utf-8')) > 255:
        raise ImportErrorValue('Nom de fichier trop long (255 octets maximum)')
    return filename

def parse_upload(content_type: str, body: bytes) -> tuple[str, bytes, str]:
    if not isinstance(content_type, str) or not content_type.lower().startswith('multipart/form-data;'):
        raise ImportErrorValue('Requête multipart/form-data requise')
    if not body or len(body) > MAX_MULTIPART_BYTES:
        raise ImportErrorValue('Requête trop volumineuse')
    # Email parser understands multipart boundaries and leaves binary payload intact.
    header = ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode('ascii')
    msg = BytesParser(policy=policy.default).parsebytes(header + body)
    if not msg.is_multipart() or msg.defects:
        raise ImportErrorValue('Formulaire multipart invalide')
    files = []
    save_path = ''
    for part in msg.iter_parts():
        field = part.get_param('name', header='Content-Disposition')
        if field == 'torrent':
            filename = part.get_filename() or ''
            try:
                validate_torrent_filename(filename)
            except ImportErrorValue as exc:
                raise ImportErrorValue('Sélectionner un fichier .torrent valide') from exc
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes) or not (1 <= len(payload) <= MAX_TORRENT_BYTES):
                raise ImportErrorValue(f'Fichier .torrent vide ou supérieur à {MAX_TORRENT_MIB} Mio')
            files.append((filename, payload))
        elif field == 'savePath':
            if save_path:
                raise ImportErrorValue('Chemin de destination fourni plusieurs fois')
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes) or len(payload) > 512:
                raise ImportErrorValue('Chemin de destination incorrect')
            try:
                save_path = payload.decode('utf-8').strip()
            except UnicodeError as exc:
                raise ImportErrorValue('Chemin de destination incorrect') from exc
            if '\x00' in save_path or '\r' in save_path or '\n' in save_path:
                raise ImportErrorValue('Chemin de destination incorrect')
    if len(files) != 1:
        raise ImportErrorValue('Un seul fichier .torrent doit être fourni')
    return files[0][0], files[0][1], save_path


def parse_upload_batch(content_type: str, body: bytes) -> list[dict]:
    """Read up to 30 independent .torrent uploads, preserving per-file errors.

    No filesystem changes occur here; callers must validate metadata separately.
    """
    if not isinstance(content_type, str) or not content_type.lower().startswith('multipart/form-data;'):
        raise ImportErrorValue('Requête multipart/form-data requise')
    if not body or len(body) > MAX_BATCH_MULTIPART_BYTES:
        raise ImportErrorValue('Lot trop volumineux (64 Mio maximum)')
    header = ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode('ascii')
    msg = BytesParser(policy=policy.default).parsebytes(header + body)
    if not msg.is_multipart() or msg.defects:
        raise ImportErrorValue('Formulaire multipart invalide')
    result = []
    total = 0
    for part in msg.iter_parts():
        field = part.get_param('name', header='Content-Disposition')
        if field != 'torrent':
            raise ImportErrorValue('Champ inattendu dans le lot')
        if len(result) >= MAX_BATCH_FILES:
            raise ImportErrorValue('30 fichiers .torrent maximum par importation')
        filename = part.get_filename() or ''
        value = part.get_payload(decode=True)
        total += len(value or b'')
        if total > MAX_BATCH_BYTES:
            raise ImportErrorValue('Lot trop volumineux (64 Mio maximum)')
        issue = None
        try:
            validate_torrent_filename(filename)
        except ImportErrorValue as exc:
            issue = str(exc)
        if issue is None and (not isinstance(value, bytes) or not (1 <= len(value) <= MAX_TORRENT_BYTES)):
            issue = f'Fichier vide ou supérieur à {MAX_TORRENT_MIB} Mio'
        result.append({'filename': filename if issue is None else filename[:180], 'payload': value if issue is None else None,
                       'error': issue})
    if not result:
        raise ImportErrorValue('Sélectionner au moins un fichier .torrent')
    return result


def torrent_content_size(info):
    files = info.get(b'files')
    if files is None:
        length = info.get(b'length')
        return length if type(length) is int and length >= 0 else None
    if not isinstance(files, list):
        return None
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            return None
        attributes = entry.get(b'attr', b'')
        if isinstance(attributes, bytes) and b'p' in attributes:
            continue
        length = entry.get(b'length')
        if type(length) is not int or length < 0:
            return None
        total += length
    return total


def inspect_torrent(payload: bytes, configured_rules: list[dict], *, require_match=True) -> dict:
    if not isinstance(payload, bytes) or len(payload) > MAX_TORRENT_BYTES or not payload:
        raise ImportErrorValue('Fichier .torrent vide ou trop volumineux')
    try:
        root, end = decode_bencode(payload)
        info_bytes = get_info_slice(payload)
    except (ValueError, IndexError, RecursionError, OverflowError) as exc:
        raise ImportErrorValue('Fichier .torrent invalide') from exc
    if end != len(payload) or not isinstance(root, dict) or not isinstance(root.get(b'info'), dict):
        raise ImportErrorValue('Métadonnées torrent invalides')
    info = root[b'info']
    version = info.get(b'meta version', 1)
    if version not in (1, 2):
        raise ImportErrorValue('Version de métadonnées non prise en charge')
    is_v2_only = version == 2 and b'pieces' not in info
    sha1 = hashlib.sha1(info_bytes).hexdigest()
    sha256 = hashlib.sha256(info_bytes).hexdigest()
    preferred_hash = sha256 if is_v2_only else sha1
    raw_name = info.get(b'name.utf-8') or info.get(b'name') or b'(sans nom)'
    name = raw_name.decode('utf-8', errors='replace')[:160] if isinstance(raw_name, bytes) else '(sans nom)'
    url_candidates = []
    single = root.get(b'announce')
    if isinstance(single, bytes):
        url_candidates.append(single)
    for tier in root.get(b'announce-list', []):
        if isinstance(tier, list):
            url_candidates.extend(url for url in tier if isinstance(url, bytes))
        elif isinstance(tier, bytes):
            url_candidates.append(tier)
    domains = set()
    for raw_url in url_candidates:
        try:
            host = domain(raw_url.decode('utf-8'))
        except UnicodeError:
            continue
        if host:
            domains.add(host)
    if not domains:
        raise ImportErrorValue('Aucun tracker HTTP(S) reconnu dans ce fichier')
    allowed_domains = set()
    for host in domains:
        if any(r.get('enabled') and any(domain_matches(host, d) for d in r.get('trackerDomains', []))
               for r in configured_rules):
            allowed_domains.add(host)
    if require_match and not allowed_domains:
        raise ImportErrorValue('Aucun domaine du torrent ne correspond à une règle de tracker active')
    return {
        'name': name, 'hash': preferred_hash, 'sizeBytes': torrent_content_size(info),
        'alternativeHashes': (sha1, sha256, sha256[:40]),
        'domains': sorted(domains), 'matchedDomains': sorted(allowed_domains),
        'isV2': is_v2_only,
    }
