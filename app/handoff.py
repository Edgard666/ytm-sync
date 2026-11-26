"""Conservative, persistent qBittorrent-first handoff state.

This module is deliberately independent of JOAL's traffic generator. It
never considers a qBittorrent-cached leecher count proof of demand, never
resumes manually stopped torrents and persists cooldowns across restarts.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import threading
import time

HASH = re.compile(r"^[0-9a-f]{40}$")
ACTIVE_SEED_STATES = frozenset({'uploading', 'stalledUP', 'queuedUP', 'forcedUP'})


def complete_running(torrent):
    try:
        return (torrent.get('state') in ACTIVE_SEED_STATES and
                float(torrent.get('progress', 0)) >= 1 and
                torrent.get('amount_left', 0) == 0)
    except (ValueError, TypeError):
        return False


def fresh_observation(snapshot, infohash, rule_id):
    """Return (leechers, unique timestamp) only for a fresh valid scrape."""
    item = snapshot.get(infohash.lower(), {}).get(rule_id, {})
    # A partial scrape is valid evidence for the responding tracker(s),
    # not a global count across all trackers. Require an actual success.
    if item.get('status') not in ('récent', 'partiel'):
        return None
    if item.get('status') == 'partiel' and not (type(item.get('successCount')) is int
                                                and item['successCount'] > 0):
        return None
    count, sample = item.get('leechers'), item.get('sampledAt')
    if type(count) is not int or count < 0 or not isinstance(sample, str):
        return None
    try:
        when = dt.datetime.fromisoformat(sample.replace('Z', '+00:00'))
        if when.tzinfo is None:
            return None
    except ValueError:
        return None
    return count, sample


def entry_confirmations_required(leechers, threshold, configured=2, margin=5):
    """Only speed up entry with a fresh, accepted tracker sample.

    Calling code must obtain the count via fresh_observation() first. A large
    margin can skip a *second* scrape, never safety or cooldown checks.
    """
    if type(leechers) is not int or leechers < 0:
        return configured
    if type(threshold) is not int or type(margin) is not int or margin < 1:
        return configured
    return 1 if leechers >= threshold + margin else configured


class HandoffLedger:
    """Persistent state per torrent. All methods are thread safe."""
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.RLock()
        self.entries = {}
        self.corrupt = False
        if self.path.is_file():
            try:
                payload = json.loads(self.path.read_text(encoding='utf-8'))
                if payload.get('version') == 1 and isinstance(payload.get('entries'), dict):
                    self.entries = {h: value for h, value in payload['entries'].items()
                                    if HASH.fullmatch(h) and isinstance(value, dict)}
                    if len(self.entries) != len(payload['entries']):
                        self.corrupt = True
                else:
                    self.corrupt = True
            except (OSError, ValueError, AttributeError, TypeError):
                self.corrupt = True
        self.recovery_pending = True

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                    json.dump({'version': 1, 'entries': self.entries}, handle,
                              ensure_ascii=False, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path)
                os.chmod(self.path, 0o600)
            except BaseException:
                try: temp.unlink()
                except OSError: pass
                raise

    def entry(self, h):
        with self.lock:
            return dict(self.entries.get(h.lower(), {}))

    def owned_hashes(self):
        with self.lock:
            return {h for h, entry in self.entries.items() if entry.get('owned') is True}

    def cooldown_remaining(self, h):
        return max(0, int(self.entry(h).get('cooldownUntil', 0) - self.clock()))

    def observe(self, h, rule, observed, threshold):
        """Count only DISTINCT tracker samples; no multiplication per poll tick."""
        if observed is None:
            return 0
        count, sample_id = observed
        state = 'high' if count >= threshold else 'low'
        with self.lock:
            e = self.entries.setdefault(h.lower(), {})
            unique = rule + ':' + sample_id
            if e.get('lastSampleId') == unique:
                return e.get('confirmations', 0) if e.get('lastDirection') == state else 0
            if e.get('lastDirection') != state or e.get('lastRule') != rule:
                e['confirmations'] = 0
            e['lastSampleId'] = unique
            e['lastRule'] = rule
            e['lastDirection'] = state
            e['lastSampleAt'] = sample_id
            e['lastLeechers'] = count
            e['confirmations'] = min(100, e.get('confirmations', 0) + 1)
            self.save()
            return e['confirmations']

    def mark_stopped_by_controller(self, h, rule):
        with self.lock:
            e = self.entries.setdefault(h.lower(), {})
            e['owned'] = True
            e['rule'] = rule
            e['stoppedByControllerAt'] = self.clock()
            e['reason'] = 'Arrêt demandé par le contrôleur après confirmations du tracker'
            self.save()

    def mark_returned(self, h, minutes, reason):
        with self.lock:
            e = self.entries.setdefault(h.lower(), {})
            now = self.clock()
            e['owned'] = False
            e['lastReturnedAt'] = now
            e['cooldownUntil'] = max(now + minutes * 60, e.get('cooldownUntil', 0))
            e['lastDirection'] = None
            e['confirmations'] = 0
            e['reason'] = reason
            self.save()

    def queue_manual(self, h, target, initial_state=None):
        """Persist a user-requested direction, without bypassing safety checks.

        A request is not an executed switch. The poll worker verifies all
        conditions and confirms completion before marking it fulfilled.
        """
        if not HASH.fullmatch(h.lower()) or target not in ('joal', 'qbit'):
            raise ValueError('Demande de basculement invalide')
        with self.lock:
            entry = self.entries.setdefault(h.lower(), {})
            previous = entry.get('manualRequest')
            if (isinstance(previous, dict) and previous.get('target') == target
                    and previous.get('status') == 'pending'):
                return dict(previous)  # Idempotent repeated click/API retry.
            entry['manualRequest'] = {
                'target': target, 'status': 'pending',
                'requestedAt': self.clock(),
                'expiresAt': self.clock() + 24 * 3600,
                'initialState': initial_state if initial_state in ('stopped', 'running') else None,
                'detail': 'Demande reçue, vérifications en attente',
            }
            self.save()
            return dict(entry['manualRequest'])

    def pending_manual(self):
        with self.lock:
            changed = False
            for entry in self.entries.values():
                request = entry.get('manualRequest')
                if (isinstance(request, dict) and request.get('status') == 'pending'
                        and request.get('expiresAt', 0) < self.clock()):
                    request.update(status='rejected', detail='Demande expirée après 24 heures',
                                   finishedAt=self.clock())
                    changed = True
            if changed:
                self.save()
            return {h: dict(e['manualRequest']) for h, e in self.entries.items()
                    if isinstance(e.get('manualRequest'), dict)
                    and e['manualRequest'].get('status') == 'pending'
                    and e['manualRequest'].get('target') in ('joal', 'qbit')}

    def finish_manual(self, h, target, status, detail):
        if status not in ('completed', 'rejected'):
            raise ValueError('Statut de demande manuel incorrect')
        with self.lock:
            entry = self.entries.get(h.lower())
            request = entry.get('manualRequest') if entry else None
            if (not isinstance(request, dict) or request.get('target') != target
                    or request.get('status') != 'pending'):
                return False  # A newer request takes priority.
            request['status'] = status
            request['detail'] = str(detail)[:240]
            request['finishedAt'] = self.clock()
            self.save()
            return True

    def manual_request(self, h):
        item = self.entry(h).get('manualRequest')
        return dict(item) if isinstance(item, dict) else None

    def remove_absent(self, visible_hashes):
        """Keep cooldowns on transient API loss; prune only old non-owned entries."""
        with self.lock:
            now = self.clock()
            expired = [h for h, e in self.entries.items()
                       if h not in visible_hashes and not e.get('owned') and
                       e.get('cooldownUntil', 0) + 86400 < now]
            for h in expired:
                self.entries.pop(h)
            if expired: self.save()

    def public_status(self, h):
        e = self.entry(h)
        return {'managed': bool(e.get('owned')),
                'cooldownRemainingSeconds': self.cooldown_remaining(h),
                'cooldownUntil': (dt.datetime.fromtimestamp(e['cooldownUntil'], dt.timezone.utc).isoformat()
                                  if e.get('cooldownUntil') else None),
                'lastReturnedAt': (dt.datetime.fromtimestamp(e['lastReturnedAt'], dt.timezone.utc).isoformat()
                                   if e.get('lastReturnedAt') else None),
                'lastScrapeAt': e.get('lastSampleAt'),
                'lastLeechers': e.get('lastLeechers'),
                'confirmations': e.get('confirmations', 0),
                'reason': e.get('reason', ''),
                'manualRequest': self.manual_request(h)}
