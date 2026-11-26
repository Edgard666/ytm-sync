"""Size-based rotation for the output of JOAL processes.

The child process writes to a pipe; a daemon thread copies it into
joal.log and rotates joal.log -> joal.log.1 -> ... -> joal.log.N. Writing
through a pipe (instead of handing the file to the child) lets the controller
rotate while the JVM is running.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path


def _env_int(name, default, low, high):
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return min(high, max(low, value))


MAX_BYTES = _env_int("LOG_MAX_MB", 5, 1, 500) * 1024 * 1024
BACKUPS = _env_int("LOG_BACKUPS", 3, 1, 20)


class RotatingLog:
    """Append-only file with numbered backups and 0600 permissions."""

    def __init__(self, path, max_bytes=None, backups=None):
        self.path = Path(path)
        self.max_bytes = max_bytes or MAX_BYTES
        self.backups = backups or BACKUPS
        self.lock = threading.Lock()
        self.stream = None
        self.size = 0
        self._open()
        # Logs written by older versions may already exceed the limit.
        if self.size >= self.max_bytes:
            self._rotate()

    def _open(self):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self.stream = os.fdopen(fd, "ab", buffering=0)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.size = self.path.stat().st_size

    def _rotate(self):
        self.stream.close()
        for index in range(self.backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{index}")
            if older.exists():
                older.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(self.path.name + ".1"))
        self._open()

    def write(self, data: bytes):
        with self.lock:
            if self.stream is None:
                return
            if self.size and self.size + len(data) > self.max_bytes:
                self._rotate()
            self.stream.write(data)
            self.size += len(data)

    def close(self):
        with self.lock:
            if self.stream is not None:
                self.stream.close()
                self.stream = None


def pump(stream, log: RotatingLog, on_line=None):
    """Copy a child's output into the rotating log until EOF (thread target)."""
    try:
        for line in iter(stream.readline, b""):
            if not isinstance(line, bytes):
                break
            try:
                log.write(line)
            except OSError:
                pass  # A full disk must not block the child on a full pipe.
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass
    finally:
        try:
            stream.close()
        finally:
            log.close()


def start_pump(stream, log, on_line=None):
    thread = threading.Thread(target=pump, args=(stream, log, on_line), name="joal-log", daemon=True)
    thread.start()
    return thread


def log_files(root):
    """Sizes of every joal.log* under runtime/ (for the health page)."""
    result = []
    root = Path(root)
    if not root.is_dir():
        return result
    for path in sorted(root.glob("*/joal.log*")):
        try:
            if path.is_file() and not path.is_symlink():
                result.append({"rule": path.parent.name, "file": path.name, "sizeBytes": path.stat().st_size})
        except OSError:
            continue
    return result
