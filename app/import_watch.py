"""Watched folder: drop .torrent files in data/import/ to add them to the
"JOAL uniquement" library without using the WebUI.

Each file is moved after processing:
  import/traités/   imported
  import/doublons/  already in the library
  import/erreurs/   rejected, with <name>.erreur.txt explaining why
Only files that have not changed for MIN_AGE seconds are read, so a file still
being copied over SMB is never imported half-written.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from torrent_import import MAX_TORRENT_BYTES

SUBFOLDERS = {"added": "traités", "duplicate": "doublons", "error": "erreurs"}
MIN_AGE = 5
MAX_PER_SCAN = 50


def ensure(folder: Path, mode=0o770):
    folder.mkdir(parents=True, exist_ok=True)
    for name in SUBFOLDERS.values():
        (folder / name).mkdir(exist_ok=True)
    for path in (folder, *(folder / name for name in SUBFOLDERS.values())):
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _destination(folder: Path, status: str, filename: str) -> Path:
    target = folder / SUBFOLDERS[status] / filename
    if not target.exists():
        return target
    stem, suffix = (filename[:-8], ".torrent") if filename.lower().endswith(".torrent") else (filename, "")
    return folder / SUBFOLDERS[status] / f"{stem}.{time.strftime('%Y%m%d-%H%M%S')}{suffix}"


def scan(folder, importer, now=None, min_age=MIN_AGE, *, observations=None):
    """Process ready .torrent files. importer(payload, filename) -> (status, message).

    status is 'added', 'duplicate' or 'error'. Returns the list of results.
    """
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        return []
    now = time.time() if now is None else now
    results = []
    paths = sorted(folder.iterdir())
    if observations is not None:
        for missing in observations.keys() - set(paths):
            del observations[missing]
    for path in paths:
        if len(results) >= MAX_PER_SCAN:
            break
        if path.is_symlink() or not path.is_file() or not path.name.lower().endswith(".torrent"):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        signature = (info.st_ino, info.st_size, info.st_mtime_ns)
        if observations is not None and observations.get(path, (None,))[0] != signature:
            observations[path] = (signature, False)
            continue
        if now - info.st_mtime < min_age:
            continue  # probably still being copied
        if info.st_size > MAX_TORRENT_BYTES or info.st_size == 0:
            status, message = "error", "Fichier vide ou trop volumineux"
        else:
            try:
                with path.open('rb') as stream:
                    payload = stream.read(MAX_TORRENT_BYTES + 1)
                current = path.stat()
                if (current.st_ino, current.st_size, current.st_mtime_ns) != signature:
                    continue
                status, message = importer(payload, path.name)
            except OSError:
                status, message = "error", "Lecture du fichier impossible"
            except Exception as exc:  # importer bug: never loop on the same file
                status, message = "error", "Import impossible (" + type(exc).__name__ + ")"
        if status not in SUBFOLDERS:
            status = "error"
        if observations is not None and status == "error" and not observations[path][1]:
            observations[path] = (signature, True)
            continue
        target = _destination(folder, status, path.name)
        try:
            current = path.stat()
            if (current.st_ino, current.st_size, current.st_mtime_ns) != signature:
                continue
            path.replace(target)
            if observations is not None:
                observations.pop(path, None)
            if status == "error":
                target.with_name(target.name + ".erreur.txt").write_text(str(message)[:500] + "\n", encoding="utf-8")
        except OSError:
            # Cannot move: leave it but report; the next scan will retry.
            message = str(message) + " (déplacement impossible)"
        results.append({"filename": path.name, "status": status, "message": str(message)[:200]})
    return results
