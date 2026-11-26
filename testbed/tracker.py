"""Minimal BitTorrent HTTP tracker for the JOAL Smart Controller test bench.

Standard library only. Supports announce (compact peers) and scrape, keeps the
swarm in memory and can imitate a private tracker:
  ALLOWED_CLIENTS=-qB,-TR   reject other peer_id prefixes ("client not allowed")
  PASSKEY=secret            require /secret/announce and /secret/scrape
Status page: GET /stats (JSON). Never use it as a real public tracker.
"""
import json
import os
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

PORT = int(os.environ.get("TRACKER_PORT", "6969"))
INTERVAL = int(os.environ.get("TRACKER_INTERVAL", "60"))
ALLOWED = tuple(p for p in os.environ.get("ALLOWED_CLIENTS", "").split(",") if p)
PASSKEY = os.environ.get("PASSKEY", "")

LOCK = threading.Lock()
SWARMS = {}  # info_hash bytes -> {peer_id bytes: {ip, port, left, last, uploaded}}
EVENTS = []  # last announces, for the /stats page


def bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, bool) or not isinstance(value, (int, list, dict)):
        raise TypeError(type(value))
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
    return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"


def raw_query(path):
    """Decode the query keeping binary values (info_hash, peer_id)."""
    query = urlsplit(path).query
    return dict(parse_qsl(query, keep_blank_values=True, encoding="latin-1"))


def counts(swarm):
    seeders = sum(1 for peer in swarm.values() if peer["left"] == 0)
    return seeders, len(swarm) - seeders


def expire(now):
    for info_hash in list(SWARMS):
        swarm = SWARMS[info_hash]
        for peer_id in [p for p, peer in swarm.items() if now - peer["last"] > INTERVAL * 2 + 30]:
            del swarm[peer_id]


class Handler(BaseHTTPRequestHandler):
    server_version = "TestTracker/1.0"

    def log_message(self, fmt, *args):  # quieter than the default access log
        pass

    def reply(self, body, status=200, content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def failure(self, reason):
        self.reply(bencode({"failure reason": reason}))

    def route(self):
        parts = [p for p in urlsplit(self.path).path.split("/") if p]
        if PASSKEY:
            if len(parts) != 2 or parts[0] != PASSKEY:
                return None
            parts = parts[1:]
        return parts[0] if len(parts) == 1 else None

    def do_GET(self):
        if urlsplit(self.path).path == "/stats":
            return self.stats()
        action = self.route()
        if action == "announce":
            return self.announce()
        if action == "scrape":
            return self.scrape()
        if PASSKEY and action is None:
            return self.failure("passkey invalid")
        self.reply(b"not found", 404)

    def announce(self):
        q = raw_query(self.path)
        info_hash = q.get("info_hash", "").encode("latin-1")
        peer_id = q.get("peer_id", "").encode("latin-1")
        if len(info_hash) != 20 or len(peer_id) != 20:
            return self.failure("invalid info_hash or peer_id")
        client = peer_id[:8].decode("latin-1")
        if ALLOWED and not client.startswith(ALLOWED):
            self.record(info_hash, client, "refused: client not allowed")
            return self.failure("client not allowed")
        try:
            port, left = int(q.get("port", "0")), int(q.get("left", "0"))
            uploaded = int(q.get("uploaded", "0"))
        except ValueError:
            return self.failure("invalid numeric parameter")
        now = time.time()
        with LOCK:
            expire(now)
            swarm = SWARMS.setdefault(info_hash, {})
            if q.get("event") == "stopped":
                swarm.pop(peer_id, None)
            else:
                swarm[peer_id] = {"ip": self.client_address[0], "port": port, "left": left,
                                  "last": now, "uploaded": uploaded, "client": client}
            seeders, leechers = counts(swarm)
            peers = b""
            for other_id, peer in swarm.items():
                if other_id == peer_id:
                    continue
                try:
                    peers += socket.inet_aton(peer["ip"]) + struct.pack("!H", peer["port"])
                except OSError:
                    continue
        self.record(info_hash, client, q.get("event") or "update", left=left, uploaded=uploaded)
        self.reply(bencode({"interval": INTERVAL, "min interval": INTERVAL, "complete": seeders,
                            "incomplete": leechers, "peers": peers}))

    def scrape(self):
        wanted = [v.encode("latin-1") for k, v in parse_qsl(urlsplit(self.path).query, keep_blank_values=True,
                                                             encoding="latin-1") if k == "info_hash"]
        files = {}
        with LOCK:
            expire(time.time())
            for info_hash in wanted or list(SWARMS):
                seeders, leechers = counts(SWARMS.get(info_hash, {}))
                files[info_hash] = {"complete": seeders, "incomplete": leechers, "downloaded": 0}
        self.reply(bencode({"files": files}))

    def record(self, info_hash, client, event, **extra):
        with LOCK:
            EVENTS.append({"time": int(time.time()), "infoHash": info_hash.hex(), "client": client,
                           "event": event, **extra})
            del EVENTS[:-200]

    def stats(self):
        with LOCK:
            expire(time.time())
            body = {"interval": INTERVAL, "allowedClients": list(ALLOWED), "passkey": bool(PASSKEY),
                    "swarms": {h.hex(): {"seeders": counts(s)[0], "leechers": counts(s)[1],
                                         "peers": [{"client": p["client"], "left": p["left"],
                                                    "uploaded": p["uploaded"]} for p in s.values()]}
                               for h, s in SWARMS.items()},
                    "recentAnnounces": EVENTS[-50:]}
        self.reply(json.dumps(body, indent=2).encode(), content_type="application/json")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Tracker de test sur :{PORT} · intervalle {INTERVAL} s · clients autorisés : "
          f"{', '.join(ALLOWED) or 'tous'} · passkey : {'oui' if PASSKEY else 'non'}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
