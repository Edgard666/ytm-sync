"""Announce identity of a JOAL .client profile, built the way JOAL builds it.

Used by the controller's own tracker readings (stopped announce when a tracker refuses
scraping), so private trackers see the same client as JOAL's sessions: peer_id prefix,
key format, query template, URL encoding and HTTP headers of the chosen profile.
Unsupported profiles raise Unsupported; callers then keep the previous generic request.
"""
import json
import re
import secrets
import threading
from pathlib import Path


class Unsupported(ValueError):
    pass


# ---- tiny generator for the regular expressions used by JOAL profiles ------------------
def _class_chars(body):
    chars, i = [], 0
    while i < len(body):
        c = body[i]
        if c == '\\' and i + 1 < len(body):
            c = body[i + 1]
            i += 1
        if i + 2 < len(body) and body[i + 1] == '-' and body[i + 2] != ']':
            end = body[i + 2]
            if end == '\\' and i + 3 < len(body):
                end = body[i + 3]
                i += 1
            chars.extend(chr(x) for x in range(ord(c), ord(end) + 1))
            i += 3
            continue
        chars.append(c)
        i += 1
    if not chars:
        raise Unsupported('classe de caractères vide')
    return chars


def _tokens(pattern):
    """List of (choices, count): each token is a set of allowed characters repeated count times."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c in '()':                      # groups only hold literals in JOAL profiles
            i += 1
            continue
        if c in '|?+*^$':
            raise Unsupported('expression non prise en charge')
        if c == '[':
            end = pattern.index(']', i + 2 if pattern[i + 1] == ']' else i + 1)
            choices = _class_chars(pattern[i + 1:end])
            i = end + 1
        elif c == '\\':
            choices = [pattern[i + 1]]
            i += 2
        elif c == '.':
            raise Unsupported('expression non prise en charge')
        else:
            choices = [c]
            i += 1
        count = 1
        quantifier = re.match(r'\{(\d+)(?:,(\d+))?\}', pattern[i:])
        if quantifier:
            low, high = int(quantifier.group(1)), int(quantifier.group(2) or quantifier.group(1))
            count = low + secrets.randbelow(high - low + 1)
            i += quantifier.end()
        out.append((choices, count))
    return out


def sample_regex(pattern):
    text = ''.join(secrets.choice(choices) for choices, count in _tokens(pattern) for _ in range(count))
    if not re.fullmatch(pattern, text, re.DOTALL):
        raise Unsupported('expression non reproduite')
    return text


# ---- identity -----------------------------------------------------------------------------
def _peer_id(spec):
    algorithm = spec['algorithm']
    kind = algorithm.get('type')
    if kind == 'REGEX':
        value = sample_regex(algorithm['pattern'])
    elif kind == 'RANDOM_POOL_WITH_CHECKSUM':
        prefix, pool, base = algorithm['prefix'], algorithm['charactersPool'], int(algorithm['base'])
        size = 20 - len(prefix)
        values = [secrets.randbelow(256) % base for _ in range(size - 1)]
        remainder = sum(values) % base
        value = prefix + ''.join(pool[v] for v in values) + pool[base - remainder if remainder else 0]
    else:
        raise Unsupported('générateur de peer_id inconnu')
    if len(value.encode('latin-1')) != 20:
        raise Unsupported('peer_id de longueur incorrecte')
    return value


def _key(spec):
    algorithm = spec['algorithm']
    kind = algorithm.get('type')
    if kind == 'HASH':
        value = secrets.token_hex(int(algorithm['length']))[:int(algorithm['length'])]
    elif kind == 'HASH_NO_LEADING_ZERO':
        value = secrets.token_hex(int(algorithm['length']))[:int(algorithm['length'])].lstrip('0') or '1'
    elif kind == 'DIGIT_RANGE_TRANSFORMED_TO_HEX_WITHOUT_LEADING_ZEROES':
        low, high = int(algorithm['inclusiveLowerBound']), int(algorithm['inclusiveUpperBound'])
        value = format(low + secrets.randbelow(high - low + 1), 'x')
    elif kind == 'REGEX':
        value = sample_regex(algorithm['pattern'])
    else:
        raise Unsupported('générateur de clé inconnu')
    case = spec.get('keyCase')
    return value.upper() if case == 'upper' else value.lower() if case == 'lower' else value


class ClientIdentity:
    PORT = 49152            # JOAL's default incoming port

    def __init__(self, profile, name='profil'):
        try:
            self.name = name
            self.query = str(profile['query'])
            self.numwant = int(profile.get('numwant', 50))
            self.numwant_on_stop = int(profile.get('numwantOnStop', 0))
            encoder = profile['urlEncoder']
            self._keep = re.compile(encoder['encodingExclusionPattern'])
            self._upper = encoder.get('encodedHexCase') == 'upper'
            self._encode_peer = bool(profile['peerIdGenerator'].get('shouldUrlEncode'))
            self._peer_spec, self._key_spec = profile['peerIdGenerator'], profile['keyGenerator']
            self.peer_id = _peer_id(self._peer_spec)               # one per controller run (JOAL: per run)
            self.headers = [(str(h['name']), str(h['value']).replace('{java}', '21').replace('{os}', 'Windows 10')
                             .replace('{locale}', 'fr-FR')) for h in profile.get('requestHeaders', [])]
        except (KeyError, TypeError, ValueError, re.error) as exc:
            raise Unsupported(f'profil {name} inexploitable') from exc
        for placeholder in re.findall(r'\{[a-z0-9_]+\}', self.query):
            if placeholder not in ('{infohash}', '{peerid}', '{port}', '{uploaded}', '{downloaded}', '{left}',
                                   '{key}', '{event}', '{numwant}', '{ip}', '{ipv6}'):
                raise Unsupported('variable de requête inconnue : ' + placeholder)
        self._keys = {}
        self._lock = threading.Lock()

    def key(self, infohash):
        """Key kept per torrent, like JOAL's TORRENT_PERSISTENT profiles."""
        with self._lock:
            if infohash not in self._keys:
                self._keys[infohash] = _key(self._key_spec)
            return self._keys[infohash]

    def encode(self, raw):
        digits = '%%%02X' if self._upper else '%%%02x'
        return ''.join(c if self._keep.fullmatch(c) else digits % ord(c) for c in raw)

    def announce_query(self, infohash, left, event='stopped'):
        values = {'{infohash}': self.encode(bytes.fromhex(infohash).decode('latin-1')),
                  '{peerid}': self.encode(self.peer_id) if self._encode_peer else self.peer_id,
                  '{port}': str(self.PORT), '{uploaded}': '0', '{downloaded}': '0', '{left}': str(left),
                  '{key}': self.key(infohash), '{event}': event,
                  '{numwant}': str(self.numwant_on_stop if event == 'stopped' else self.numwant)}
        parts = []
        for part in self.query.split('&'):
            if '{ip}' in part or '{ipv6}' in part or (not event and '{event}' in part):
                continue                       # JOAL drops unknown addresses and an empty event
            for name, value in values.items():
                part = part.replace(name, value)
            parts.append(part)
        return '&'.join(parts)

    def udp_identity(self, infohash):
        """peer_id bytes and 32-bit key for a BEP 15 announce."""
        return self.peer_id.encode('latin-1'), int(self.key(infohash), 16) & 0xFFFFFFFF if re.fullmatch(
            r'[0-9a-fA-F]{1,8}', self.key(infohash)) else secrets.randbits(32)


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def load(path):
    """Identity of a .client file, cached while the file is unchanged."""
    path = Path(path)
    stamp = path.stat().st_mtime_ns
    with _CACHE_LOCK:
        cached = _CACHE.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
    try:
        profile = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise Unsupported(f'profil {path.name} illisible') from exc
    identity = ClientIdentity(profile, path.name)
    with _CACHE_LOCK:
        _CACHE[str(path)] = (stamp, identity)
    return identity
