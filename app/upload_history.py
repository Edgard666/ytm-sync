"""Seven days of timestamped JOAL rates, committed in small batches."""
import sqlite3
from contextlib import closing

RETENTION = 7 * 86400


def store(path, samples):
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (time INTEGER PRIMARY KEY, rate INTEGER)')
        db.executemany('INSERT OR REPLACE INTO samples VALUES (?, ?)', samples)
        db.execute('DELETE FROM samples WHERE time < ?', (samples[-1][0] - RETENTION,))


def window(path, start, end):
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
        return db.execute('SELECT time, rate FROM samples WHERE time BETWEEN ? AND ? ORDER BY time',
                          (start, end)).fetchall()


def session_volumes(path, samples, *, totals=False):
    """One durable high-water mark per torrent and JVM session."""
    if not samples and (not totals or not path.exists()):
        return {}
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS volumes (hash TEXT, session TEXT, bytes INTEGER NOT NULL, '
                   'PRIMARY KEY (hash, session))')
        if samples:
            db.executemany('INSERT INTO volumes VALUES (?, ?, ?) ON CONFLICT(hash, session) '
                           'DO UPDATE SET bytes=excluded.bytes WHERE excluded.bytes > volumes.bytes', samples)
        if totals:
            return dict(db.execute('SELECT hash, SUM(bytes) FROM volumes GROUP BY hash'))
    return {}
