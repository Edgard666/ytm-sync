"""Read-only operational audit trail for JOAL Smart Controller.

No tracker URLs, credentials or fabricated per-torrent transfer counters are stored.
Operational failures in this optional layer must not change seeding decisions.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
import threading
from pathlib import Path

_HASH = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")
# Rule ids also cover Forced runners (":forced", "solo-:forced"); stored as JSON text only.
_RULE_ID = re.compile(r"^[a-zA-Z0-9_.:-]{1,80}$")
EVENT_LIMIT = 1500
SESSION_LIMIT = 400


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, ensure_ascii=False, separators=(',', ':'))
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        if temp.exists():
            temp.unlink()


class DiagnosticsStore:
    """Bounded, persistent telemetry; does not make scheduling decisions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.events = []
        self.sessions = []
        self.running = {}
        self.previous_status = {}
        self.load_error = False
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(raw, dict):
                    raise ValueError('Invalid diagnostic archive')
                self.events = list(raw.get('events', []))[-EVENT_LIMIT:]
                self.sessions = list(raw.get('sessions', []))[-SESSION_LIMIT:]
                self.previous_status = dict(raw.get('previousStatus', {}))
                for item in self.sessions:
                    if isinstance(item, dict) and item.get('status') == 'running':
                        item['status'] = 'unknown_after_restart'
                        item['endedAt'] = None
                        item['durationSeconds'] = None
        except (ValueError, TypeError, OSError):
            # Diagnostics corruption must not prevent qBittorrent fail-safe checks.
            self.load_error = True
            self.events, self.sessions, self.previous_status = [], [], {}
        self._save()

    def _save(self):
        _atomic_json(self.path, {
            'formatVersion': 1,
            'events': self.events[-EVENT_LIMIT:],
            'sessions': self.sessions[-SESSION_LIMIT:],
            'previousStatus': self.previous_status,
        })

    def event(self, code, message, infohash=None, rule=None, level='info'):
        if not _SAFE_ID.fullmatch(str(code)):
            raise ValueError('Code de journal invalide')
        if infohash is not None and not _HASH.fullmatch(str(infohash).lower()):
            raise ValueError('Infohash invalide')
        if rule is not None and not _RULE_ID.fullmatch(str(rule)):
            raise ValueError('Identifiant de règle invalide')
        if level not in ('info', 'warning', 'error'):
            raise ValueError('Niveau invalide')
        # Message text must always be a constant supplied by trusted code.
        event = {'time': iso_now(), 'code': code, 'message': str(message)[:160], 'level': level,
                 'infoHash': infohash.lower() if infohash else None, 'rule': rule}
        with self.lock:
            self.events.append(event)
            self.events = self.events[-EVENT_LIMIT:]
            self._save()
        return event

    def start_rule(self, rule, hashes):
        if not _RULE_ID.fullmatch(str(rule)):
            raise ValueError('Invalid rule id')
        now = iso_now()
        with self.lock:
            added = False
            for h in hashes:
                if not _HASH.fullmatch(str(h).lower()):
                    continue
                key = (rule, h.lower())
                if key in self.running:
                    continue
                entry = {'rule': rule, 'infoHash': h.lower(), 'startedAt': now, 'endedAt': None,
                         'durationSeconds': None, 'status': 'running', 'simulatedBytes': None}
                self.sessions.append(entry)
                self.running[key] = entry
                self.events.append({'time': now, 'code': 'joal.started', 'message': 'Session JOAL démarrée',
                                    'level': 'info', 'infoHash': h.lower(), 'rule': rule})
                added = True
            if added:
                self.sessions = self.sessions[-SESSION_LIMIT:]
                self.events = self.events[-EVENT_LIMIT:]
                self._save()

    def stop_rule(self, rule, reason='Session JOAL arrêtée'):
        now = iso_now()
        with self.lock:
            matches = [key for key in self.running if key[0] == rule]
            for key in matches:
                item = self.running.pop(key)
                item['status'] = 'stopped'
                item['endedAt'] = now
                try:
                    t1 = dt.datetime.fromisoformat(item['startedAt'])
                    t2 = dt.datetime.fromisoformat(now)
                    item['durationSeconds'] = max(0, round((t2 - t1).total_seconds()))
                except (ValueError, TypeError):
                    item['durationSeconds'] = None
                self.events.append({'time': now, 'code': 'joal.stopped', 'message': reason[:160],
                                    'level': 'info', 'infoHash': key[1], 'rule': rule})
            if matches:
                self.events = self.events[-EVENT_LIMIT:]
                self._save()

    def observe(self, rows):
        """Save only transitions, not every polling tick. Do not persist tracker credentials."""
        with self.lock:
            current = {}
            changed = False
            for row in rows:
                h = str(row.get('infoHash', '')).lower()
                if not _HASH.fullmatch(h):
                    continue
                state = ('selected' if row.get('selected') else
                         'eligible' if row.get('eligible') else 'excluded')
                current[h] = state
                previous = self.previous_status.get(h)
                if previous is not None and previous != state:
                    changed = True
                    self.events.append({'time': iso_now(), 'code': 'torrent.state',
                                        'message': f'État : {previous} → {state}', 'level': 'info',
                                        'infoHash': h, 'rule': row.get('rule') if _RULE_ID.fullmatch(str(row.get('rule'))) else None})
            # Persist (with fsync) only on a real change. A steady state used to
            # rewrite diagnostics.json every poll cycle, wearing the disk for nothing.
            if changed or current != self.previous_status:
                self.previous_status = current
                self.events = self.events[-EVENT_LIMIT:]
                self._save()
            else:
                self.previous_status = current

    def snapshot(self, infohash=None, limit=120):
        if infohash is not None and not _HASH.fullmatch(str(infohash).lower()):
            raise ValueError('Infohash invalide')
        limit = min(max(int(limit), 1), 300)
        with self.lock:
            events = [v.copy() for v in reversed(self.events) if infohash is None or v.get('infoHash') == infohash.lower()][:limit]
            sessions = [v.copy() for v in reversed(self.sessions) if infohash is None or v.get('infoHash') == infohash.lower()][:limit]
        return {'events': events, 'sessions': sessions, 'loadWarning': self.load_error,
                'limits': {'events': EVENT_LIMIT, 'sessions': SESSION_LIMIT},
                'joalTransferCounterAvailable': False,
                'joalCounterDetail': 'JOAL sans API de mesures internes activée : volume annoncé non mesuré.'}
