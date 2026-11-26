"""Read-only HTTP(S)/UDP tracker scrapes (BEP 48/BEP 15).

A scrape is NOT an announce and makes no claims about transferred bytes.  A
missing / invalid result is never interpreted as zero leechers.  The module
uses the original tracker host, refuses redirects and does not expose tokens.
"""
from __future__ import annotations

import ipaddress
import os
import gzip
import re
import socket
import struct
import secrets
import urllib.error
import urllib.parse
import urllib.request
from core import decode_bencode, domain, domain_matches

# The public diagnostics API must never expose raw tracker URLs, query strings,
# response bodies, passkeys or arbitrary exception text.
KNOWN_ERRORS = frozenset({
    'Tracker HTTP(S) non pris en charge', 'Scrape uniquement disponible pour infohash v1',
    'Tracker sans URL de scrape standard', 'Redirection refusée',
    'Résolution DNS du tracker impossible', 'Adresse réseau du tracker non publique',
    'Tracker inaccessible (scrape)', 'Réponse du tracker trop grande',
    'Scrape absent, incomplet ou non pris en charge', 'Tracker UDP invalide',
    'Port UDP invalide', 'Port UDP manquant',
    'Résolution DNS du tracker UDP impossible',
    'Adresse réseau du tracker UDP non publique',
    'Réponse UDP connect incomplète', 'Réponse UDP connect invalide',
    'Réponse UDP scrape absente ou invalide',
    'Scrape UDP non pris en charge ou incohérent',
    'Tracker UDP inaccessible (scrape)', 'Protocole du tracker non pris en charge',
    'Annonce d’arrêt sans compteurs', 'Compteur de leechers invalide', 'Tracker UDP : réponse d’erreur',
    'Délai de réponse du tracker dépassé',
    'Réponse HTTP 400 (requête rejetée)', 'Réponse HTTP 401 (authentification requise)',
    'Réponse HTTP 403 (accès refusé)', 'Réponse HTTP 404 (scrape absent)',
    'Réponse HTTP 405 (méthode non autorisée)', 'Réponse HTTP 429 (limitation des requêtes)',
    'Réponse HTTP 500 (erreur tracker)', 'Réponse HTTP 502 (erreur tracker)',
    'Réponse HTTP 503 (tracker indisponible)', 'Réponse HTTP 504 (délai côté tracker)',
})

def safe_scrape_error(error):
    """Map a failure to a finite set of public, non-secret explanations."""
    reason = str(error)
    return reason if reason in KNOWN_ERRORS else 'Réponse scrape inexploitable'

MAX_RESPONSE = 256 * 1024
UDP_PROTOCOL_ID = 0x41727101980
UDP_TIMEOUT = 7.0

class ScrapeUnavailable(ValueError):
    pass


def scrape_url(announce: str, infohash: str) -> str:
    """Return the standard scrape URL, preserving original query parameters."""
    parsed = urllib.parse.urlsplit(announce)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ScrapeUnavailable('Tracker HTTP(S) non pris en charge')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', infohash):
        raise ScrapeUnavailable('Scrape uniquement disponible pour infohash v1')
    path = parsed.path
    if 'announce' not in path:
        raise ScrapeUnavailable('Tracker sans URL de scrape standard')
    path = path.replace('announce', 'scrape', 1)
    # urlencode would modify passkey/query values. Preserve them as received.
    query = parsed.query + ('&' if parsed.query else '') + 'info_hash=' + urllib.parse.quote_from_bytes(bytes.fromhex(infohash), safe='')
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, ''))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScrapeUnavailable('Redirection refusée')


def private_trackers_allowed():
    """Test bench only (ALLOW_PRIVATE_TRACKERS=1): accept trackers on a LAN or
    Docker network. Off by default: tracker URLs come from untrusted .torrent
    files and must not be able to reach internal services."""
    return os.environ.get('ALLOW_PRIVATE_TRACKERS', '0') == '1'


def address_allowed(text):
    address = ipaddress.ip_address(text)
    if address.is_global:
        return True
    return private_trackers_allowed() and not (address.is_multicast or address.is_unspecified)


def public_ip_only(host, port):
    """Reject private, loopback, link-local, multicast and reserved tracker targets."""
    try:
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ScrapeUnavailable('Résolution DNS du tracker impossible') from exc
    if not addrs or any(not address_allowed(item[4][0]) for item in addrs):
        raise ScrapeUnavailable('Adresse réseau du tracker non publique')


def fetch_scrape(announce_url: str, infohash: str, *, opener=None, stats=False):
    """Return a fresh incomplete count, or raise. Never returns a guessed zero.

    DNS is checked before the request; only use on administrator-approved
    tracker domains. A changing DNS reply could still be a risk (DNS rebinding).
    """
    target = scrape_url(announce_url, infohash)
    parsed = urllib.parse.urlsplit(target)
    if opener is None:
        public_ip_only(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    req = urllib.request.Request(target, headers={'User-Agent': 'JOAL-Smart-Cleanup/1.7', 'Accept': 'application/x-bittorrent'})
    try:
        with opener.open(req, timeout=10) as res:
            if getattr(res, 'status', 200) != 200:
                raise ScrapeUnavailable('Statut HTTP du scrape incorrect')
            body = res.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        codes = {400, 401, 403, 404, 405, 429, 500, 502, 503, 504}
        descriptions = {400:'requête rejetée',401:'authentification requise',403:'accès refusé',
                        404:'scrape absent',405:'méthode non autorisée',429:'limitation des requêtes',
                        500:'erreur tracker',502:'erreur tracker',503:'tracker indisponible',504:'délai côté tracker'}
        if exc.code in codes:
            raise ScrapeUnavailable(f'Réponse HTTP {exc.code} ({descriptions[exc.code]})') from exc
        raise ScrapeUnavailable('Tracker inaccessible (scrape)') from exc
    except (TimeoutError, socket.timeout) as exc:
        raise ScrapeUnavailable('Délai de réponse du tracker dépassé') from exc
    except (OSError, urllib.error.URLError) as exc:
        if isinstance(getattr(exc, 'reason', None), TimeoutError):
            raise ScrapeUnavailable('Délai de réponse du tracker dépassé') from exc
        raise ScrapeUnavailable('Tracker inaccessible (scrape)') from exc
    if len(body) > MAX_RESPONSE:
        raise ScrapeUnavailable('Réponse du tracker trop grande')
    try:
        payload, end = decode_bencode(body)
        if end != len(body) or not isinstance(payload, dict):
            raise ValueError('Données bencode invalides')
        record = payload[b'files'][bytes.fromhex(infohash)]
        count = record.get(b'incomplete')
        if type(count) is not int or count < 0:
            count = None
        seeders = record.get(b'complete')
        if type(seeders) is not int or seeders < 0:
            seeders = None
        if count is None and (not stats or seeders is None):
            raise ValueError('Compteur indisponible')
        return {'leechers': count, 'seeders': seeders} if stats else count
    except (ValueError, KeyError, IndexError, TypeError, RecursionError) as exc:
        raise ScrapeUnavailable('Scrape absent, incomplet ou non pris en charge') from exc



def fetch_udp_scrape(announce_url: str, infohash: str, *, timeout=UDP_TIMEOUT, resolver=None, socket_factory=None, stats=False, stopped_left=None, presence=None, udp_identity=None):
    """BEP 15 connect + scrape only. No announce or peer registration.

    Strict response checks prevent mismatched or truncated UDP packets being
    mistaken for a zero-leech result. Only publicly routable endpoints are
    permitted and the socket is connected to the selected resolved address.
    """
    parsed = urllib.parse.urlsplit(announce_url)
    if parsed.scheme.lower() != 'udp' or not parsed.hostname or parsed.username or parsed.password:
        raise ScrapeUnavailable('Tracker UDP invalide')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', infohash):
        raise ScrapeUnavailable('Scrape uniquement disponible pour infohash v1')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ScrapeUnavailable('Port UDP invalide') from exc
    if not port:
        raise ScrapeUnavailable('Port UDP manquant')
    resolver = resolver or socket.getaddrinfo
    socket_factory = socket_factory or socket.socket
    try:
        addresses = resolver(parsed.hostname, port, type=socket.SOCK_DGRAM, proto=socket.IPPROTO_UDP)
    except (OSError, ValueError) as exc:
        raise ScrapeUnavailable('Résolution DNS du tracker UDP impossible') from exc
    if not addresses or any(not address_allowed(address[4][0]) for address in addresses):
        raise ScrapeUnavailable('Adresse réseau du tracker UDP non publique')
    family, kind, proto, _, destination = addresses[0]
    try:
        with socket_factory(family, kind, proto) as conn:
            conn.settimeout(timeout)
            conn.connect(destination)
            connect_txn = secrets.randbits(32)
            conn.sendall(struct.pack('!QII', UDP_PROTOCOL_ID, 0, connect_txn))
            response = conn.recv(2048)
            if len(response) < 16:
                raise ScrapeUnavailable('Réponse UDP connect incomplète')
            action, txn, connection_id = struct.unpack('!IIQ', response[:16])
            if action != 0 or txn != connect_txn or len(response) != 16:
                raise ScrapeUnavailable('Réponse UDP connect invalide')
            if stopped_left is not None:
                txn = secrets.randbits(32)
                peer, key, event = presence or ((*udp_identity, 'stopped') if udp_identity
                                                else (secrets.token_bytes(20), secrets.randbits(32), 'stopped'))
                event_id = {'started': 2, 'stopped': 3, '': 0}[event]
                packet = struct.pack('!QII20s20sQQQIIIiH', connection_id, 1, txn, bytes.fromhex(infohash),
                                     peer, 0, stopped_left, 0, event_id, 0, key, 0, 49152 if udp_identity else 6881)
                path = (parsed.path + ('?' + parsed.query if parsed.query else '')).encode('ascii')
                for start in range(0, len(path), 255):
                    chunk = path[start:start + 255]
                    packet += bytes((2, len(chunk))) + chunk
                conn.sendall(packet)
                reply = conn.recv(2048)
                if presence and len(reply) >= 8 and struct.unpack('!II', reply[:8]) == (3, txn):
                    raise AnnounceRejected(rejection_category(reply[8:]))
                if len(reply) < 20:
                    raise ScrapeUnavailable('Annonce d’arrêt sans compteurs')
                action, echoed, interval, leechers, seeders = struct.unpack('!IIIII', reply[:20])
                if action != 1 or echoed != txn or (not presence and leechers == 0xffffffff):
                    raise ScrapeUnavailable('Annonce d’arrêt sans compteurs')
                result = {'leechers': None if leechers == 0xffffffff else leechers, 'seeders': None if seeders == 0xffffffff else seeders}
                if presence:
                    result['interval'] = interval
                return result
            scrape_txn = secrets.randbits(32)
            conn.sendall(struct.pack('!QII20s', connection_id, 2, scrape_txn, bytes.fromhex(infohash)))
            response = conn.recv(2048)
            if len(response) >= 8 and struct.unpack('!I', response[:4])[0] == 3:
                raise ScrapeUnavailable('Tracker UDP : réponse d’erreur')
            if len(response) != 20:
                raise ScrapeUnavailable('Réponse UDP scrape absente ou invalide')
            action, txn, complete, downloaded, incomplete = struct.unpack('!IIIII', response)
            if action != 2 or txn != scrape_txn or (incomplete == 0xffffffff and (not stats or complete == 0xffffffff)):
                raise ScrapeUnavailable('Scrape UDP non pris en charge ou incohérent')
            seeders = complete if complete != 0xffffffff else None
            return {'leechers': None if incomplete == 0xffffffff else incomplete, 'seeders': seeders} if stats else incomplete
    except ScrapeUnavailable:
        raise
    except (TimeoutError, socket.timeout) as exc:
        raise ScrapeUnavailable('Délai de réponse du tracker dépassé') from exc
    except (OSError, struct.error, ValueError, OverflowError) as exc:
        raise ScrapeUnavailable('Tracker UDP inaccessible (scrape)') from exc


def fetch_tracker_scrape(announce_url: str, infohash: str) -> int:
    scheme = urllib.parse.urlsplit(announce_url).scheme.lower()
    if scheme == 'udp':
        return fetch_udp_scrape(announce_url, infohash)
    if scheme in ('http', 'https'):
        return fetch_scrape(announce_url, infohash)
    raise ScrapeUnavailable('Protocole du tracker non pris en charge')

def fetch_tracker_stats(announce_url: str, infohash: str):
    """One scrape request for two informational counters (complete/incomplete).

    This does not announce, register a peer or change selection policy. The
    counters can describe one tracker only; they must never be summed across
    unrelated trackers or represented as a global deduplicated total.
    """
    scheme = urllib.parse.urlsplit(announce_url).scheme.lower()
    if scheme == 'udp':
        return fetch_udp_scrape(announce_url, infohash, stats=True)
    if scheme in ('http', 'https'):
        return fetch_scrape(announce_url, infohash, stats=True)
    raise ScrapeUnavailable('Protocole du tracker non pris en charge')


def import_announce_stats(url, infohash, left, identity=None):
    """One stopped announce for an imported torrent. Never register a running session."""
    return zero_transfer_announce(url, infohash, left, identity=identity)


class AnnounceRejected(RuntimeError):
    """Only a fixed public category may leave the tracker transport."""


def rejection_category(reason):
    message = reason.lower() if isinstance(reason, bytes) else b''
    for keywords, label in [
        ((b'passkey', b'auth', b'credential'), 'Authentification refusée'),
        ((b'unregistered torrent', b'unknown torrent', b'torrent not found'), 'Torrent inconnu du tracker'),
        ((b'client', b'peer id', b'peer_id'), 'Client refusé par le tracker'),
        ((b'too frequent', b'too soon', b'rate limit'), 'Annonces trop fréquentes'),
    ]:
        if any(word in message for word in keywords):
            return label
    return 'Refus explicite du tracker'


def announce_category(ok, message):
    """Map JOAL's announce outcome to a fixed public label (no URL, no passkey)."""
    if ok:
        return 'Acceptée'
    text = str(message or '').lower()
    status = re.search(r'status (\d{3})', text)
    for keywords, label in [
        (('too many announces failed',), 'Échecs répétés : torrent écarté par JOAL'),
        (('passkey', 'auth', 'credential', 'not authorized', 'unauthorized'), 'Authentification refusée'),
        (('unregistered', 'unknown torrent', 'torrent not found', 'not registered', 'not exist'), 'Torrent inconnu du tracker'),
        (('client', 'peer id', 'peer_id', 'banned'), 'Client refusé par le tracker'),
        (('too frequent', 'too soon', 'rate limit', 'slow down'), 'Annonces trop fréquentes'),
        (('no response', 'timed out', 'timeout', 'connection', 'connect', 'refused', 'unknownhost', 'no more valid tracker'), 'Tracker injoignable'),
        (('protocol', 'reading tracker', 'read tracker'), 'Réponse du tracker illisible'),
    ]:
        if any(word in text for word in keywords):
            return label
    if status:
        return f'Réponse HTTP {status.group(1)}'
    return 'Refus ou erreur du tracker'


def zero_transfer_announce(url, infohash, left, presence=None, identity=None):
    """Announce without transfer. `identity` (client_identity.ClientIdentity) makes the
    controller's own stopped announce look like the JOAL profile (v1.29.4)."""
    if type(left) is not int or not 0 <= left < 2**63 or not re.fullmatch(r'[0-9a-fA-F]{40}', infohash):
        raise ScrapeUnavailable('Annonce d’arrêt sans compteurs')
    if presence is not None:
        identity = None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == 'udp':
        return fetch_udp_scrape(url, infohash, stopped_left=left, presence=presence,
                                udp_identity=identity.udp_identity(infohash) if identity else None)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None:
        raise ScrapeUnavailable('Tracker HTTP(S) non pris en charge')
    peer, key, event = presence or (secrets.token_bytes(20), None, 'stopped')
    existing = {name for name, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)}
    headers = {}
    if identity is not None:
        added = identity.announce_query(infohash, left, 'stopped')
        names = {part.split('=', 1)[0] for part in added.split('&')}
        headers = dict(identity.headers)
    else:
        params = dict(info_hash=bytes.fromhex(infohash), peer_id=peer, port=6881,
                      uploaded=0, downloaded=0, left=left, compact=1, numwant=0)
        if key is not None and 'key' not in existing:
            params['key'] = key
        if event:
            params['event'] = event
        added = urllib.parse.urlencode(params)
        names = {*params, 'event'}
    if existing.intersection(names):
        raise ScrapeUnavailable('Annonce d’arrêt sans compteurs')
    public_ip_only(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    query = (parsed.query + '&' if parsed.query else '') + added
    target = parsed._replace(query=query, fragment='').geturl()
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(target, headers=headers) if headers else target
        with opener.open(request, timeout=10) as response:
            body = response.read(MAX_RESPONSE + 1)
            encoding = getattr(getattr(response, 'headers', None), 'get', lambda *_: '')('Content-Encoding', '') or ''
            if headers and encoding.lower() == 'gzip' and len(body) <= MAX_RESPONSE:
                try:
                    body = gzip.decompress(body)[:MAX_RESPONSE + 1]
                except (OSError, EOFError):
                    raise ScrapeUnavailable('Annonce d’arrêt sans compteurs') from None
    except urllib.error.HTTPError as exc:
        if not presence:
            raise
        label = 'motif non précisé'
        try:
            with exc:
                body = exc.read(8193)
            if len(body) <= 8192:
                data, end = decode_bencode(body)
                if end == len(body) and isinstance(data, dict) and b'failure reason' in data:
                    label = rejection_category(data[b'failure reason'])
        except (ValueError, OSError, IndexError, RecursionError):
            pass
        raise AnnounceRejected(f'HTTP {exc.code} · {label}') from None
    if len(body) <= MAX_RESPONSE:
        try:
            data, end = decode_bencode(body)
            if presence and end == len(body) and isinstance(data, dict) and b'failure reason' in data:
                raise AnnounceRejected(rejection_category(data[b'failure reason']))
            if end == len(body) and isinstance(data, dict) and b'failure reason' not in data:
                count, seeds = data.get(b'incomplete'), data.get(b'complete')
                if presence:
                    interval = data.get(b'interval')
                    if (type(interval) is int and interval > 0) or event == 'stopped' or (interval is None and any(k in data for k in (b'peers', b'peers6', b'complete', b'incomplete'))):
                        minimum = data.get(b'min interval')
                        return {'interval': max(interval if type(interval) is int else 1800,
                                                minimum if type(minimum) is int else 0),
                                'leechers': count if type(count) is int and count >= 0 else None,
                                'seeders': seeds if type(seeds) is int and seeds >= 0 else None}
                if not presence and type(count) is int and count >= 0:
                    return {'leechers': count, 'seeders': seeds if type(seeds) is int and seeds >= 0 else None}
        except (ValueError, IndexError, RecursionError):
            pass
    raise ScrapeUnavailable('Annonce d’arrêt sans compteurs')


def checked_scrapes(trackers, rules, infohash: str, fetch=None):
    """Require a successful fresh response from ALL matched unique trackers.

    Returns (count, num_trackers). Private keys in URLs never leave this function.
    """
    fetch = fetch or fetch_tracker_scrape
    relevant = []
    for item in trackers:
        url = item.get('url')
        host = domain(url) if isinstance(url, str) else None
        if host and any(r.get('enabled') and any(domain_matches(host, name) for name in r['trackerDomains']) for r in rules):
            if url not in relevant:
                relevant.append(url)
    if not relevant:
        raise ScrapeUnavailable('Aucun tracker HTTP(S)/UDP autorisé')
    if len(relevant) > 8:
        raise ScrapeUnavailable('Trop de trackers pour une vérification complète')
    counts = [fetch(url, infohash) for url in relevant]
    return max(counts), len(counts)


def observed_scrapes(trackers, rules, infohash: str, fetch=None, *, details=False, diagnostics=False, stats_details=False, max_urls=8):
    """Read every allowlisted tracker; retain partial observations for display.

    Return (maximum_known, successful, expected). A partial scrape is never
    evidence for destructive cleanup or stopping real seeding.
    """
    fetch = fetch or (fetch_tracker_stats if stats_details else fetch_tracker_scrape)
    relevant = []
    for item in trackers:
        url = item.get('url')
        host = domain(url) if isinstance(url, str) else None
        if host and any(r.get('enabled') and any(domain_matches(host, pat) for pat in r['trackerDomains'])
                        for r in rules):
            if url not in relevant:
                relevant.append(url)
    if not relevant or len(relevant) > max_urls:
        raise ScrapeUnavailable('Aucun tracker autorisé ou trop de trackers')
    counts = []
    seed_counts = []
    by_tracker_seeds = {domain(url): None for url in relevant}
    by_tracker = {domain(url): None for url in relevant}
    explanations = {domain(url): {'state':'inconnu', 'reason':'Aucun résultat'} for url in relevant}
    for url in relevant:
        host = domain(url)
        try:
            measured = fetch(url, infohash)
            count = measured.get('leechers') if isinstance(measured, dict) else measured
            seeders = measured.get('seeders') if isinstance(measured, dict) else None
            if type(seeders) is int and seeders >= 0:
                seed_counts.append(seeders)
                by_tracker_seeds[host] = max(seeders, by_tracker_seeds[host] or 0)
            if type(count) is not int or count < 0:
                raise ScrapeUnavailable('Compteur de leechers invalide')
            counts.append(count)
            by_tracker[host] = max(count, by_tracker[host]) if by_tracker[host] is not None else count
            explanations[host] = {'state':'ok', 'reason':'Relevé reçu'}
        except (ScrapeUnavailable, OSError, ValueError, TimeoutError) as exc:
            # A second URL from the same host may still work. Keep success.
            if explanations[host]['state'] != 'ok':
                explanations[host] = {'state':'error', 'reason':safe_scrape_error(exc)}
    result = (max(counts) if counts else None, len(counts), len(relevant))
    if details and diagnostics and stats_details:
        return (*result, by_tracker, explanations, max(seed_counts) if seed_counts else None, by_tracker_seeds)
    if details and diagnostics:
        return (*result, by_tracker, explanations)
    return (*result, by_tracker) if details else result
