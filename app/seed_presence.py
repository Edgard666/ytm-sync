"""Announce complete, idle torrents. Only tracker control traffic is generated."""
import json
import logging
from pathlib import Path
import secrets
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from urllib.error import HTTPError
from core import domain
from solo_torrents import announce_urls
from tracker_scrape import AnnounceRejected, fetch_tracker_stats, zero_transfer_announce


def presence_counts(url, infohash, response, alternatives=(), stop=None):
    counts = [response.get(key) for key in ('seeders', 'leechers')]
    for target in dict.fromkeys((url, *alternatives)):
        if None not in counts or (stop is not None and stop.is_set()):
            break
        try:
            extra = fetch_tracker_stats(target, infohash)
        except (ValueError, OSError):
            extra = {}
        counts = [extra.get(key) if value is None else value
                  for key, value in zip(('seeders', 'leechers'), counts)]
    return [value if value is not None else -1 for value in counts]


def run(folder, telemetry):
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    lock = threading.Lock()
    samples = {}

    def session(path):
        h = path.stem
        urls = announce_urls(path.read_bytes())
        if not urls:
            return
        peer, key = secrets.token_bytes(20), secrets.randbits(32)
        active = None
        index = 0
        next_scrape = 0
        outcomes = {}

        def record(url, result):
            with lock:
                outcomes[domain(url)] = {'time': int(time.time()), 'result': result}
                values = samples.setdefault(h, [0, 0, -1, -1, 0, outcomes])
                values[5] = outcomes
        try:
            while not stop.is_set():
                url = active or urls[index % len(urls)]
                try:
                    response = zero_transfer_announce(url, h, 0, (peer, key, '' if active else 'started'))
                    record(url, 'Acceptée')
                    active = url
                    if not stop.is_set():
                        counts = presence_counts(url, h, response, urls, stop)
                        with lock:
                            samples[h] = [0, 0, *counts, int(time.time()), outcomes]
                    delay = max(60, response['interval'])
                except (ValueError, OSError, AnnounceRejected) as exc:
                    if isinstance(exc, HTTPError):
                        result = f'HTTP {exc.code}'
                    elif isinstance(exc, AnnounceRejected):
                        result = str(exc)
                    elif isinstance(exc, OSError):
                        result = 'Connexion impossible ou interrompue'
                    else:
                        result = 'Réponse ou adresse d’annonce inexploitable'
                    record(url, result)
                    logging.warning('Annonce de présence refusée ou indisponible (%s) : %s', h[:12], result)
                    if not stop.is_set() and time.monotonic() >= next_scrape:
                        counts = presence_counts(url, h, {}, urls, stop)
                        if counts != [-1, -1]:
                            with lock:
                                samples[h] = [0, 0, *counts, int(time.time()), outcomes]
                        next_scrape = time.monotonic() + 900
                    index += 1
                    delay = 60
                stop.wait(delay)
        finally:
            if active:
                try:
                    zero_transfer_announce(active, h, 0, (peer, key, 'stopped'))
                except (ValueError, OSError, AnnounceRejected):
                    logging.warning('Annonce de fin indisponible (%s)', h[:12])

    paths = list(folder.glob('*.torrent'))
    if not paths:
        return
    with ThreadPoolExecutor(max_workers=len(paths)) as workers:
        def report(future):
            # Executor futures swallow exceptions: log them so a dead session is visible.
            if not future.cancelled() and future.exception() is not None:
                logging.error('Session de présence interrompue', exc_info=future.exception())
        for path in paths:
            workers.submit(session, path).add_done_callback(report)
        staging = telemetry.with_suffix('.tmp')
        while not stop.is_set():
            with lock:
                staging.write_text(json.dumps(samples))
            staging.replace(telemetry)
            stop.wait(1)


if __name__ == '__main__':
    run(Path(sys.argv[1]), Path(sys.argv[2]))
