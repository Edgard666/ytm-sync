"""Minimal qBittorrent Web API v2 client, including explicit start/stop."""
import http.cookiejar
import urllib.error
import json
import urllib.parse
import urllib.request
import secrets
import re


class QBitClient:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("QBIT_URL doit être HTTP(S)")
        self.user = user
        self.password = password
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged = False

    def request(self, path: str, params=None, method="GET") -> bytes:
        url = self.url + "/api/v2/" + path
        encoded = urllib.parse.urlencode(params or {})
        data = encoded.encode() if method == "POST" else None
        if method == "GET" and encoded:
            url += "?" + encoded
        req = urllib.request.Request(url, data=data, headers={"Referer": self.url + "/", "User-Agent": "JOAL-Governor/1.0"}, method=method)
        with self.opener.open(req, timeout=20) as response:
            return response.read()

    def login(self):
        response = self.request("auth/login", {"username": self.user, "password": self.password}, "POST")
        if response.strip() != b"Ok.":
            raise RuntimeError("Authentification qBittorrent refusée")
        self.logged = True

    def call(self, path, params=None, method="GET"):
        # WebUI may allow local/Docker subnets without authentication. Probe a
        # read-only endpoint first so no credentials are required in that case.
        if not self.logged:
            try:
                self.request("app/version")
            except urllib.error.HTTPError as exc:
                if exc.code not in (401, 403):
                    raise
                self.login()
            else:
                self.logged = True
        try:
            return self.request(path, params, method)
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403):
                raise
            # Session expired, or access policy changed since the probe.
            self.logged = False
            self.login()
            return self.request(path, params, method)

    def add_tag(self, hash_, tag):
        self.call("torrents/addTags", {"hashes": hash_, "tags": tag}, method="POST")

    def remove_tag(self, hash_, tag):
        self.call("torrents/removeTags", {"hashes": hash_, "tags": tag}, method="POST")

    def stop_torrent(self, hash_):
        """Stop (5.x) or pause (4.x); never removes data."""
        if not isinstance(hash_, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", hash_):
            raise ValueError("Infohash invalide")
        return self._call_versioned("torrents/stop", "torrents/pause", hash_)

    def start_torrent(self, hash_):
        """Start (5.x) or resume (4.x); call only for a torrent stopped by this controller."""
        if not isinstance(hash_, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", hash_):
            raise ValueError("Infohash invalide")
        return self._call_versioned("torrents/start", "torrents/resume", hash_)

    def _call_versioned(self, modern, legacy, hash_):
        """qBittorrent 5.x renamed pause/resume to stop/start (404 on 4.x)."""
        if getattr(self, "legacy_api", False):
            return self.call(legacy, {"hashes": hash_}, method="POST")
        try:
            return self.call(modern, {"hashes": hash_}, method="POST")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            self.legacy_api = True
            return self.call(legacy, {"hashes": hash_}, method="POST")

    def torrents_all(self):
        """Inventory for duplicate detection (no tag constraint)."""
        return json.loads(self.call("torrents/info"))

    def add_torrent_file(self, payload: bytes, filename: str, tag: str, save_path: str = ""):
        """Add one torrent to qBittorrent, stopped, without bypassing file validation."""
        if not payload or not filename.lower().endswith('.torrent'):
            raise ValueError('Fichier .torrent incorrect')
        boundary = '----joal-import-' + secrets.token_hex(18)
        chunks = []
        def add_field(name, value):
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode('utf-8'))
        add_field('paused', 'true')
        add_field('tags', tag)
        if save_path:
            add_field('savepath', save_path)
        # Don't reflect filename in a MIME header: a fixed safe name suffices.
        chunks.extend([
            f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; filename="import.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode('ascii'),
            payload,
            f'\r\n--{boundary}--\r\n'.encode('ascii')
        ])
        request_body = b''.join(chunks)
        if not self.logged:
            self.call('app/version')
        req_headers = {
            'Referer': self.url + '/', 'User-Agent': 'JOAL-Governor/1.0',
            'Content-Type': f'multipart/form-data; boundary={boundary}'
        }
        def send():
            req = urllib.request.Request(self.url + '/api/v2/torrents/add',
                                         data=request_body, headers=req_headers, method='POST')
            with self.opener.open(req, timeout=30) as response:
                return response.read()
        try:
            result = send()
        except urllib.error.HTTPError as exc:
            if exc.code not in (401, 403):
                raise
            self.logged = False
            self.login()
            result = send()
        if result.strip() not in (b'', b'Ok.'):
            # Recent versions may return JSON with detailed result metadata.
            try:
                response = json.loads(result)
                if not isinstance(response, dict) or response.get('failure_count', 0) or response.get('success_count', 0) < 1:
                    raise RuntimeError('qBittorrent n’a pas accepté le torrent')
            except (ValueError, TypeError):
                raise RuntimeError('Réponse d’import qBittorrent inattendue')
        return result

    def delete_torrent_with_data(self, hash_):
        """Permanently remove one torrent and its files; only called after opt-in checks."""
        if not isinstance(hash_, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", hash_):
            raise ValueError("Infohash invalide pour la suppression")
        return self.call("torrents/delete", {"hashes": hash_, "deleteFiles": "true"}, method="POST")

    def torrents(self, tag, *, field="tag"):
        if field not in ("tag", "category"):
            raise ValueError("Filtre qBittorrent invalide")
        data = json.loads(self.call("torrents/info", {field: tag}))
        return [t for t in data if t.get("hash")]

    def trackers(self, hash_):
        return json.loads(self.call("torrents/trackers", {"hash": hash_}))

    def export(self, hash_):
        return self.call("torrents/export", {"hash": hash_})
