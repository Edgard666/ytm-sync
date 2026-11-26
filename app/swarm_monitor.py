"""Autonomous, read-only tracker swarm observations (not BitTorrent announces).

After qBittorrent provides an infohash and tracker announce URL, this module
can continue read-only HTTP(S)/UDP scrapes without using qBittorrent.  It does not
open any BitTorrent peer port, claim uploaded bytes, or make JOAL decisions.

The encrypted-by-tracker passkeys inside announce URLs are secrets. They stay
in a private file, never in dashboard JSON or logs.  Loss of qBittorrent
connectivity does not turn old metadata into permission to start JOAL.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path

from core import domain, domain_matches
from tracker_scrape import fetch_tracker_stats, import_announce_stats, observed_scrapes, ScrapeUnavailable, safe_scrape_error

HASH = re.compile(r'^[0-9a-f]{40}$')
FORCED_MIN_GAP = 60  # seconds: anti-hammering for repeated manual mode changes
PEER_LOG_EVERY = 900  # seconds between two identical announce points
HISTORY_LIMIT = 1344  # approx. 14 days at a 15-minute interval
HISTORY_AGE = 14 * 86400

class SwarmMonitor:
    def __init__(self, path, now=time.time):
        self.path = Path(path)
        self.now = now
        self.lock = threading.RLock()
        self.peer_dirty = False
        self.pending_imports = set()
        self.forced_refresh = set()  # hashes to scrape now, ignoring the usual interval
        # infohash -> client_identity.ClientIdentity (or None) for the controller's stopped announces.
        self.identity_for = None
        self.entries = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding='utf-8'))
                if payload.get('version') == 1 and isinstance(payload.get('entries'), dict):
                    for h, item in payload['entries'].items():
                        if HASH.fullmatch(h) and isinstance(item, dict) and isinstance(item.get('urls'), list):
                            # Fetches remain subject to the tracker network checks.
                            self.entries[h] = item
            except (OSError, ValueError, AttributeError):
                pass

    def save(self):
        """Atomic write with owner-only permissions; URLs may contain private passkeys."""
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    json.dump({'version': 1, 'entries': self.entries}, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
            except Exception:
                try: tmp.unlink()
                except OSError: pass
                raise

    def synchronize(self, torrents, trackers_for_hash, tag):
        """Only qBittorrent provides admission/removal of tagged torrent records.

        Call only after a successful full inventory fetch. On connection loss,
        do not remove entries; observations can continue independently.
        """
        incoming = {str(t.get('hash', '')).lower(): t for t in torrents
                    if HASH.fullmatch(str(t.get('hash', '')).lower())}
        modified = False
        with self.lock:
            for h in set(self.entries) - set(incoming):
                if self.entries[h].get('mode') == 'solo':
                    continue
                del self.entries[h]
                modified = True
            for h, t in incoming.items():
                old = self.entries.get(h)
                if old is not None and old.get('mode') == 'solo':
                    continue  # The standalone library owns this metadata
                if old is None or not old.get('urls') or self.now() - float(old.get('urlsRefreshedAt', 0) or 0) >= 3600:
                    try:
                        raw = trackers_for_hash(h)
                        urls = sorted({tr.get('url') for tr in raw if isinstance(tr, dict) and
                                       isinstance(tr.get('url'), str) and domain(tr['url'])})[:8]
                    except (OSError, ValueError, RuntimeError):
                        # A transient qBittorrent tracker query failure should not
                        # prevent storing other records during this refresh.
                        continue
                    self.entries[h] = {'name': str(t.get('name', '(sans nom)'))[:160],
                                       'urls': urls, 'observations': old.get('observations', {}) if old else {},
                                       'history': old.get('history', {}) if old else {},
                                       'trackerHistory': old.get('trackerHistory', {}) if old else {},
                                       'lastPeers': old.get('lastPeers', {}) if old else {},
                                       'peerLog': old.get('peerLog', []) if old else [],
                                       'addedAt': old.get('addedAt', self.now()) if old else self.now(),
                                       'seenQbitAt': self.now(), 'urlsRefreshedAt': self.now()}
                    modified = True
                else:
                    name = str(t.get('name', '(sans nom)'))[:160]
                    if old.get('name') != name:
                        old['name'] = name
                        modified = True
                    # Keep previous tracker URLs until explicit torrent/tag removal;
                    # no need to re-query qBittorrent to perform later scrapes.
                    old['seenQbitAt'] = self.now()
        if modified:
            self.save()
        return modified

    def remember_peers(self, h, counts, sampled, source):
        """Display-only observations, independent of eligibility and rule changes."""
        if not sampled:
            return
        with self.lock:
            record = self.entries.get(h)
            if not record:
                return
            last = record.setdefault('lastPeers', {})
            for key in ('seeders', 'leechers'):
                count = counts.get(key)
                if type(count) is not int or count < 0 or sampled < last.get(key, {}).get('time', 0):
                    continue
                value = {'count': count, 'time': sampled, 'source': source}
                if last.get(key) != value:
                    last[key] = value
                    self.peer_dirty = True
            # Chart history of announce-reported counts: one point per change,
            # plus at least one every PEER_LOG_EVERY seconds while unchanged.
            log = record.setdefault('peerLog', [])
            point = {'time': int(sampled), 'source': str(source)[:40],
                     'seeders': last.get('seeders', {}).get('count'),
                     'leechers': last.get('leechers', {}).get('count')}
            previous = log[-1] if log else None
            if (previous is None or sampled > previous.get('time', 0) and (
                    (previous.get('seeders'), previous.get('leechers')) != (point['seeders'], point['leechers'])
                    or sampled - previous.get('time', 0) >= PEER_LOG_EVERY)):
                log.append(point)
                cutoff = sampled - HISTORY_AGE
                record['peerLog'] = [p for p in log if isinstance(p, dict) and p.get('time', 0) >= cutoff][-HISTORY_LIMIT:]
                self.peer_dirty = True

    def display_peers(self, hashes):
        with self.lock:
            return {h: {k: dict(v) for k, v in self.entries[h]['lastPeers'].items()}
                    for h in hashes if self.entries.get(h, {}).get('lastPeers')}

    def flush_peers(self):
        with self.lock:
            if self.peer_dirty:
                self.save()
                self.peer_dirty = False

    def register_solo(self, infohash, name, urls, size=None):
        """Persist metadata for independent tracker monitoring (no qBittorrent)."""
        h = str(infohash).lower()
        if not HASH.fullmatch(h):
            raise ValueError('Infohash v1 incorrect')
        safe_urls = sorted({u for u in urls if isinstance(u, str) and domain(u)})[:32]
        if not safe_urls:
            raise ValueError('Aucun tracker pris en charge')
        with self.lock:
            old = self.entries.get(h, {})
            if old.get('mode') == 'solo' and old.get('urls') == safe_urls:
                if size is not None:
                    old['sizeBytes'] = size
                return False
            self.pending_imports.add(h)
            self.entries[h] = {
                'mode': 'solo', 'name': str(name)[:160], 'urls': safe_urls, 'sizeBytes': size,
                'lastPeers': old.get('lastPeers', {}),
                'peerLog': old.get('peerLog', []),
                'observations': old.get('observations', {}),
                'history': old.get('history', {}),
                'trackerHistory': old.get('trackerHistory', {}),
                'addedAt': old.get('addedAt', self.now()),
                'urlsRefreshedAt': self.now(),
            }
            self.save()
        return True

    def request_refresh(self, hashes):
        """Scrape these torrents at the next pass, before any other, even if
        checked recently (at most once per FORCED_MIN_GAP seconds per torrent).
        Returns the hashes actually queued (known to the monitor)."""
        with self.lock:
            known = {str(h).lower() for h in hashes} & set(self.entries)
            self.forced_refresh.update(known)
            self.pending_imports.update(known)
        return known

    def stale_hashes(self, max_age_seconds):
        """Torrents without any successful sample in the last max_age_seconds."""
        cutoff = self.now() - max_age_seconds
        with self.lock:
            result = set()
            for h, record in self.entries.items():
                samples = [o.get('sampledAt') for o in record.get('observations', {}).values()
                           if isinstance(o, dict) and isinstance(o.get('sampledAt'), (int, float))]
                if not samples or max(samples) < cutoff:
                    result.add(h)
            return result

    def refresh_pending(self, infohash):
        with self.lock:
            return str(infohash).lower() in self.forced_refresh

    def forget_solo(self, infohash):
        h = str(infohash).lower()
        with self.lock:
            if self.entries.get(h, {}).get('mode') != 'solo':
                return False
            del self.entries[h]
            self.pending_imports.discard(h)
            self.save()
        return True

    def refresh(self, rules, *, max_checks=20, min_age=900, fetch=None, overrides=None, on_sample=None, import_probe=None):
        """Refresh the least-recently checked torrent/rule pairs first.

        Earlier versions visited the first 20 infohashes at every interval,
        potentially starving later entries indefinitely. This keeps a bounded
        request budget but ensures fair rotation through large libraries.
        """
        if not isinstance(rules, list):
            return 0
        jobs = []
        overrides = overrides or {}
        now = self.now()
        with self.lock:
            solo_hashes = {h for h, record in self.entries.items() if record.get('mode') == 'solo'}
            for h, record in self.entries.items():
                urls = list(record.get('urls', []))
                covered = set()
                for rule in rules:
                    if not rule.get('enabled'):
                        continue
                    chosen_id = overrides.get(h)
                    if chosen_id:
                        if rule['id'] != chosen_id:
                            continue
                        matches = [url for url in urls if domain(url)]
                        # This policy is explicitly assigned to one torrent.
                        # A virtual allowlist is confined to that torrent's URLs.
                        effective_rule = dict(rule, trackerDomains=sorted({domain(u) for u in matches}))
                    else:
                        matches = [url for url in urls if domain(url) and any(
                            domain_matches(domain(url), pat)
                            for pat in rule.get('trackerDomains', []))]
                        effective_rule = rule
                    if not matches:
                        continue
                    covered.update(matches)
                    rid = str(rule['id'])
                    old = record.get('observations', {}).get(rid, {})
                    checked_at = old.get('attemptedAt')
                    if isinstance(checked_at, (int, float)) and now - checked_at < (
                            FORCED_MIN_GAP if h in self.forced_refresh else min_age):
                        continue
                    priority = checked_at if isinstance(checked_at, (int, float)) else float('-inf')
                    jobs.append((priority, h, rid, matches, effective_rule))
                remaining = [url for url in urls if url not in covered]
                if record.get('mode') == 'solo' and remaining:
                    # Imported metadata can be observed without selecting a simulation rule.
                    rid = ':import'
                    last = record.get('observations', {}).get(rid, {}).get('attemptedAt')
                    if last is None or now - last >= (FORCED_MIN_GAP if h in self.forced_refresh else min_age):
                        targets = remaining
                        scope = {'enabled': True, 'trackerDomains': [domain(url) for url in targets]}
                        jobs.append((last if last is not None else float('-inf'), h, rid, targets, scope))
            urgent = self.pending_imports.copy()
            due = {job[1] for job in jobs}
            self.pending_imports.difference_update(urgent - due)
            # A forced request with nothing due (checked < FORCED_MIN_GAP ago) is satisfied.
            self.forced_refresh.difference_update(self.forced_refresh - due)
        jobs.sort(key=lambda job: (job[1] not in urgent, job[0], job[1], job[2]))
        attempted = 0
        for _, h, rid, matches, rule in jobs[:max_checks]:
            with self.lock:
                if self.pending_imports - urgent:
                    break  # A newer import arrived while a tracker request was running.
            attempted += 1
            tick = self.now()
            try:
                matching = [{'url': url} for url in matches]
                source = 'scrape HTTP(S)/UDP'
                read_stats = fetch or fetch_tracker_stats
                def import_stats(url, infohash):
                    nonlocal source
                    standalone = infohash in solo_hashes
                    scraped, failure = None, None
                    try:
                        scraped = read_stats(url, infohash)
                    except ScrapeUnavailable as exc:
                        # v1.29.2: for a torrent outside qBittorrent, ANY refused or failed scrape
                        # (timeout, HTTP 5xx, failure reason...) falls back to the stopped announce.
                        if not standalone and str(exc) not in {
                            'Tracker sans URL de scrape standard', 'Scrape absent, incomplet ou non pris en charge',
                            'Réponse HTTP 404 (scrape absent)', 'Réponse HTTP 405 (méthode non autorisée)',
                            'Réponse HTTP 403 (accès refusé)', 'Tracker UDP : réponse d’erreur',
                            'Scrape UDP non pris en charge ou incohérent', 'Réponse UDP scrape absente ou invalide'
                        }:
                            raise
                        failure = exc
                    if failure is None and (not standalone or (isinstance(scraped, dict)
                                                               and type(scraped.get('leechers')) is int)):
                        return scraped
                    # Scrape failed, or answered without a leecher count (seeders only).
                    if not import_probe(infohash):
                        if failure is not None:
                            raise failure
                        return scraped
                    with self.lock:
                        size = self.entries.get(infohash, {}).get('sizeBytes')
                    identity = self.identity_for(infohash) if self.identity_for else None
                    try:
                        # v1.29.4: same client identity as the JOAL sessions when a profile exists.
                        result = (import_announce_stats(url, infohash, size, identity=identity) if identity
                                  else import_announce_stats(url, infohash, size))
                    except (ScrapeUnavailable, OSError, ValueError):
                        if failure is not None:
                            raise failure
                        return scraped                 # keep the seeders the scrape did give
                    source = 'Relevé tracker à l’import (annonce d’arrêt)'
                    return result
                # v1.29.1: a standalone torrent whose tracker refuses scraping is read by a
                # stopped announce at every due pass, not only right after its import.
                probe = import_probe and (h in urgent or h in solo_hashes) and rid != ':import'
                amount, successful, expected, tracker_details, error_details, seeders, tracker_seeds = observed_scrapes(
                    matching, [rule], h, details=True, diagnostics=True, stats_details=True,
                    fetch=import_stats if probe else fetch, max_urls=32 if h in solo_hashes else 8)
                observation = {'attemptedAt': tick,
                               'sampledAt': tick if successful or seeders is not None else None,
                               'leechers': amount if successful else None,
                               'seeders': seeders,
                               'trackerCount': expected, 'successCount': successful,
                               'status': 'ok' if successful == expected else ('partial' if successful or seeders is not None else 'scrape indisponible'),
                               'source': source,
                               'trackerDiagnostics': error_details}
            except (ScrapeUnavailable, OSError, ValueError) as exc:
                tracker_details = {}
                tracker_seeds = {}
                error_details = {domain(url): {'state':'error', 'reason': safe_scrape_error(exc)} for url in matches if domain(url)}
                observation = {'attemptedAt': tick, 'sampledAt': None, 'leechers': None, 'seeders': None,
                               'trackerCount': len(matches), 'successCount': 0, 'status': 'scrape indisponible',
                               'source': 'scrape HTTP(S)/UDP', 'trackerDiagnostics': error_details}
            with self.lock:
                current = self.entries.get(h)
                if current is not None:
                    self.remember_peers(h, observation, observation.get('sampledAt'),
                                        'Scrape' if source == 'scrape HTTP(S)/UDP' else source)
                    current.setdefault('observations', {})[rid] = observation
                    per_tracker = current.setdefault('trackerHistory', {}).setdefault(rid, {})
                    for host, count in tracker_details.items():
                        if not host or len(host) > 200:
                            continue
                        series = per_tracker.setdefault(host, [])
                        series.append({'time': tick, 'leechers': count,
                                       'seeders': tracker_seeds.get(host),
                                       'status': 'ok' if count is not None else 'indisponible'})
                        per_tracker[host] = [point for point in series
                                             if isinstance(point, dict) and point.get('time', 0) >= tick - HISTORY_AGE][-HISTORY_LIMIT:]
                    history = current.setdefault('history', {}).setdefault(rid, [])
                    # A failed scrape is recorded as an unknown point, never zero.
                    history.append({'time': tick, 'leechers': observation['leechers'], 'seeders': observation['seeders'],
                                    'status': observation['status'],
                                    'successCount': observation['successCount'],
                                    'trackerCount': observation['trackerCount']})
                    current['history'][rid] = [point for point in history
                                               if isinstance(point, dict) and isinstance(point.get('time'), (int, float))
                                               and point['time'] >= tick - HISTORY_AGE][-HISTORY_LIMIT:]
            self.save()
            if on_sample is not None and h in urgent:
                on_sample()
        with self.lock:
            self.pending_imports.difference_update(urgent - {job[1] for job in jobs[attempted:]})
            self.forced_refresh.difference_update({job[1] for job in jobs[:attempted]})
        return attempted

    def snapshot(self, max_age_seconds=3600, *, limit=1000):
        """Privacy-safe API snapshot: no announce URL, passkey or client identity."""
        now = self.now()
        with self.lock:
            entries = []
            for h, item in self.entries.items():
                observations = {}
                for rule, o in item.get('observations', {}).items():
                    sampled = o.get('sampledAt')
                    fresh = isinstance(sampled, (int, float)) and 0 <= now - sampled <= max_age_seconds
                    observations[rule] = {
                        'leechers': o.get('leechers') if fresh and type(o.get('leechers')) is int else None,
                        'seeders': o.get('seeders') if fresh and type(o.get('seeders')) is int else None,
                        'sampledAt': dt.datetime.fromtimestamp(sampled, dt.timezone.utc).isoformat() if fresh else None,
                        'status': ('récent' if o.get('status') == 'ok' else 'partiel') if fresh
                                  else ('indisponible' if o.get('status') not in ('ok', 'partial') else 'périmé'),
                        'trackerCount': o.get('trackerCount', 0),
                        'successCount': o.get('successCount', o.get('trackerCount', 0) if o.get('status') == 'ok' else 0),
                        'source': o.get('source', 'scrape HTTP(S)/UDP') if fresh else None,
                        'attemptedAt': dt.datetime.fromtimestamp(o['attemptedAt'], dt.timezone.utc).isoformat()
                                       if isinstance(o.get('attemptedAt'), (int, float)) else None,
                        'trackerDiagnostics': [
                            {'domain': host, 'state': detail.get('state', 'inconnu'),
                             'reason': ('Relevé reçu' if detail.get('state') == 'ok' else safe_scrape_error(detail.get('reason', 'Non disponible'))),
                             'seeders': (item.get('trackerHistory', {}).get(rule, {}).get(host, [])[-1].get('seeders')
                                         if item.get('trackerHistory', {}).get(rule, {}).get(host) and
                                            detail.get('state') == 'ok' and fresh else None),
                             'leechers': (item.get('trackerHistory', {}).get(rule, {}).get(host, [])[-1].get('leechers')
                                          if item.get('trackerHistory', {}).get(rule, {}).get(host) and
                                             detail.get('state') == 'ok' and fresh else None)}
                            for host, detail in sorted(o.get('trackerDiagnostics', {}).items())
                            if isinstance(host, str) and isinstance(detail, dict) and len(host) <= 200
                        ],
                    }
                hosts = sorted({domain(url) for url in item.get('urls', []) if domain(url)})
                entries.append({'infoHash': h, 'hash': h[:12], 'name': item.get('name', '(sans nom)'),
                                'trackers': hosts[:8], 'rules': observations})
            return {'count': len(entries), 'items': entries[:limit],
                    'source': 'Relevés indépendants HTTP(S)/UDP', 'updatedAt':
                        dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()}

    def peer_series(self, infohash):
        """Seeders/leechers over time for one torrent, all sources merged.

        Scrape points come from every rule's history (successful samples only);
        announce points come from JOAL or presence sessions. Unknown values stay
        None: the chart draws a gap, never a zero.
        """
        h = str(infohash).lower()
        if not HASH.fullmatch(h):
            raise ValueError('Torrent invalide')
        def count(value):
            return value if type(value) is int and value >= 0 else None
        with self.lock:
            record = self.entries.get(h)
            if record is None:
                raise KeyError('Torrent inconnu')
            points = {}
            for series in record.get('history', {}).values():
                for p in series if isinstance(series, list) else []:
                    if not isinstance(p, dict) or not isinstance(p.get('time'), (int, float)):
                        continue
                    if p.get('status') not in ('ok', 'partial', 'récent', 'partiel'):
                        continue
                    key = (int(p['time']), 'Scrape')
                    old = points.get(key, {})
                    seeders, leechers = count(p.get('seeders')), count(p.get('leechers'))
                    points[key] = {k: max((v for v in (old.get(k), new) if v is not None), default=None)
                                   for k, new in (('seeders', seeders), ('leechers', leechers))}
            for p in record.get('peerLog', []):
                if isinstance(p, dict) and isinstance(p.get('time'), (int, float)):
                    points[(int(p['time']), str(p.get('source', 'Annonce'))[:40])] = {
                        'seeders': count(p.get('seeders')), 'leechers': count(p.get('leechers'))}
            # Extend the curve to the latest known values (unchanged counters are
            # only logged every PEER_LOG_EVERY seconds).
            last = record.get('lastPeers', {})
            latest = max((v.get('time', 0) for v in last.values() if isinstance(v, dict)), default=0)
            if latest and all(latest > t for t, _ in points):
                source = next((v.get('source') for v in last.values() if isinstance(v, dict) and v.get('time') == latest), 'Annonce')
                points[(int(latest), str(source)[:40])] = {k: count(last.get(k, {}).get('count')) for k in ('seeders', 'leechers')}
            name = str(record.get('name', ''))[:160]
        ordered = [{'time': dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(), 'source': source, **values}
                   for (t, source), values in sorted(points.items())
                   if values['seeders'] is not None or values['leechers'] is not None]
        return {'infoHash': h, 'name': name, 'points': ordered[-HISTORY_LIMIT:], 'retentionDays': HISTORY_AGE // 86400}

    def history(self, infohash, rule_id, limit=1344):
        """Read-only history for one (hash, rule), stripped of private URLs."""
        h = str(infohash).lower()
        if not HASH.fullmatch(h) or not isinstance(rule_id, str) or not rule_id or len(rule_id) > 80:
            raise ValueError('Torrent ou règle invalide')
        with self.lock:
            record = self.entries.get(h)
            if record is None:
                raise KeyError('Torrent inconnu')
            history = record.get('history', {}).get(rule_id, [])
            tracker_series = {}
            for host, series in record.get('trackerHistory', {}).get(rule_id, {}).items():
                if not isinstance(host, str) or len(host) > 200 or not isinstance(series, list):
                    continue
                tracker_series[host] = [{'time': dt.datetime.fromtimestamp(point['time'], dt.timezone.utc).isoformat(),
                                         'leechers': point.get('leechers') if type(point.get('leechers')) is int else None,
                                         'status': str(point.get('status', 'inconnu'))[:30]}
                                        for point in series[-min(max(int(limit), 1), HISTORY_LIMIT):]
                                        if isinstance(point, dict) and isinstance(point.get('time'), (int, float))]
            return {'infoHash': h, 'name': str(record.get('name', ''))[:160], 'rule': rule_id,
                    'trackerSeries': tracker_series,
                    'points': [{'time': dt.datetime.fromtimestamp(point['time'], dt.timezone.utc).isoformat(),
                                'leechers': point.get('leechers') if type(point.get('leechers')) is int else None,
                                'status': str(point.get('status', 'inconnu'))[:30],
                                'successCount': point.get('successCount', 0),
                                'trackerCount': point.get('trackerCount', 0)}
                               for point in history[-min(max(int(limit), 1), HISTORY_LIMIT):]
                               if isinstance(point, dict) and isinstance(point.get('time'), (int, float))],
                    'retentionDays': 14}
