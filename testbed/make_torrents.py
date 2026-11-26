"""Create sample files and matching .torrent files for the test bench.

Usage (variables, no arguments needed):
  python3 make_torrents.py
Environment overrides:
  ANNOUNCE   default http://tracker:6969/announce  (Docker service name)
  COUNT      default 3
  OUT_DIR    default ./generated  (data/ and torrents/ inside)
"""
import hashlib
import os
import secrets
from pathlib import Path

ANNOUNCE = os.environ.get("ANNOUNCE", "http://tracker:6969/announce")
COUNT = int(os.environ.get("COUNT", "3"))
OUT_DIR = Path(os.environ.get("OUT_DIR", Path(__file__).resolve().parent / "generated"))
PIECE = 256 * 1024


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"


def make(index):
    name = f"testbed-sample-{index:02d}.bin"
    content = secrets.token_bytes(PIECE * 2 + 1234)
    (OUT_DIR / "data").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "data" / name).write_bytes(content)
    pieces = b"".join(hashlib.sha1(content[i:i + PIECE]).digest() for i in range(0, len(content), PIECE))
    info = {"name": name, "length": len(content), "piece length": PIECE, "pieces": pieces}
    torrent = bencode({"announce": ANNOUNCE, "created by": "JOAL Smart Controller testbed", "info": info})
    (OUT_DIR / "torrents").mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "torrents" / (name + ".torrent")
    path.write_bytes(torrent)
    return path, hashlib.sha1(bencode(info)).hexdigest()


if __name__ == "__main__":
    for i in range(1, COUNT + 1):
        path, infohash = make(i)
        print(f"{path.name}  {infohash}")
    print(f"Fichiers dans {OUT_DIR} · annonce : {ANNOUNCE}")
