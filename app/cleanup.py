"""Conservative, opt-in qBittorrent cleanup based on repeated tracker reports.

These readings may be cached by qBittorrent, particularly while stopped.
The ledger records observations, *not* verified real-time swarm activity.
No files are touched here: actual deletion is separately guarded in main.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from core import domain, domain_matches, is_complete_stopped


def matches_tag(torrent, tag):
    raw = torrent.get('tags', '')
    return isinstance(raw, str) and tag in {part.strip() for part in raw.split(',')}


def assess(torrent, trackers, rules):
    if not is_complete_stopped(torrent):
        return False, 'Torrent incomplet ou actif dans qBittorrent', None
    domains = []
    counts = []
    for item in trackers:
        name = domain(item.get('url')) if isinstance(item.get('url'), str) else None
        if not name or not any(r.get('enabled') and any(domain_matches(name, d) for d in r['trackerDomains']) for r in rules):
            continue
        domains.append(name)
        count = item.get('num_leeches')
        if item.get('status') != 2 or type(count) is not int or count < 0:
            return False, 'Tracker ciblé inactif ou nombre de leechers inconnu', None
        counts.append(count)
    if not domains:
        return False, 'Aucun tracker autorisé', None
    if any(count > 0 for count in counts):
        return False, f'{max(counts)} leecher(s) signalé(s) — compteur remis à zéro', max(counts)
    return True, '0 leecher sur tous les trackers ciblés (valeurs potentiellement en cache)', 0


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(data, dict) and data.get('version') == 1 and isinstance(data.get('entries'), dict):
                    self.entries = data['entries']
            except (ValueError, OSError):
                # A corrupt ledger never authorizes cleanup.
                self.entries = {}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'version': 1, 'entries': self.entries}, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def observe(self, hash_, passed, now, max_gap_seconds, source=None):
        """Persist a continuous chain of actual API reads; reset on missing observations."""
        key = hash_.lower()
        if not passed:
            self.entries.pop(key, None)
            return None
        old = self.entries.get(key)
        if (not isinstance(old, dict) or
                type(old.get('lastSeen')) not in (float, int) or
                type(old.get('zeroSince')) not in (float, int) or
                (source is not None and old.get('source') != source) or
                now < old['lastSeen'] or now - old['lastSeen'] > max_gap_seconds):
            old = {'zeroSince': now, 'lastSeen': now, 'observations': 1, 'source': source}
        else:
            old['lastSeen'] = now
            old['observations'] = min(1000000, int(old.get('observations', 0)) + 1)
        self.entries[key] = old
        return old

    def forget_absent(self, hashes):
        active = {item.lower() for item in hashes}
        for key in list(self.entries):
            if key not in active:
                del self.entries[key]


def time_until(ledger_entry, days, now):
    if not ledger_entry:
        return None
    return max(0, int(ledger_entry['zeroSince'] + days * 86400 - now))
