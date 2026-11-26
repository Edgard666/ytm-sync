"""Persistent, in-app operational alerts. No external delivery and no secrets."""
from __future__ import annotations
import datetime as dt
import json
import os
import threading
from pathlib import Path

MAX_ALERTS = 250


def _stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class NotificationCenter:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.items = []
        self.states = {}
        self.load_error = False
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if data.get('version') != 1 or not isinstance(data.get('items'), list) or not isinstance(data.get('states'), dict):
                    raise ValueError('format non reconnu')
                self.items = [x for x in data['items'] if isinstance(x, dict)][-MAX_ALERTS:]
                self.states = {k: bool(v) for k,v in data['states'].items() if isinstance(k, str) and len(k) <= 64}
        except (ValueError, OSError, TypeError):
            self.load_error = True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.tmp')
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'items': self.items, 'states': self.states}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def observe(self, key, failing, failure_text, recovery_text):
        """Create an alert only upon an incident/recovery transition."""
        if not isinstance(key, str) or len(key) > 64 or not key:
            raise ValueError('clé invalide')
        with self.lock:
            before = self.states.get(key, False)
            failing = bool(failing)
            if before == failing:
                return False
            self.states[key] = failing
            self.items.append({'id': os.urandom(12).hex(), 'at': _stamp(),
                               'category': key, 'level': 'error' if failing else 'success',
                               'message': str(failure_text if failing else recovery_text)[:200],
                               'read': False})
            self.items = self.items[-MAX_ALERTS:]
            self._save()
            return True

    def snapshot(self):
        with self.lock:
            return {'items': list(reversed(self.items[-100:])),
                    'unread': sum(1 for item in self.items if not item.get('read')),
                    'loadWarning': self.load_error}

    def acknowledge(self, ids=None):
        """No IDs means mark all read. Caller must authenticate and pass CSRF."""
        with self.lock:
            selected = None if ids is None else set(ids)
            count = 0
            for item in self.items:
                if not item.get('read') and (selected is None or item.get('id') in selected):
                    item['read'] = True
                    count += 1
            if count:
                self._save()
            return count
