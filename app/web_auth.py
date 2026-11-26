"""Cookie-based, same-origin web authentication for a single-user LAN dashboard.

Sessions are in memory; a container restart intentionally requires logging in again.
Do not expose this UI to the public Internet without TLS and access controls.
"""
import hashlib
import hmac
import secrets
import threading
import time
from http.cookies import SimpleCookie, CookieError

SESSION_IDLE_SECONDS = 8 * 3600
SESSION_ABSOLUTE_SECONDS = 24 * 3600
COOKIE_NAME = 'joal_session'


class SessionManager:
    def __init__(self, password_getter=lambda: ''):
        self.password_getter = password_getter
        self.lock = threading.RLock()
        self.sessions = {}  # hashed bearer token -> {csrf, created, active}
        self.failures = {}  # address -> consecutive failure count and block until

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode('ascii')).digest()

    def _prune(self, now):
        for key, session in list(self.sessions.items()):
            if now - session['active'] > SESSION_IDLE_SECONDS or now - session['created'] > SESSION_ABSOLUTE_SECONDS:
                self.sessions.pop(key, None)
        for addr, item in list(self.failures.items()):
            if now - item['last'] > 900:
                self.failures.pop(addr, None)

    def create(self, username, password, remote):
        now = time.monotonic()
        addr = str(remote)[:128]
        with self.lock:
            self._prune(now)
            record = self.failures.get(addr, {'count': 0, 'until': 0})
            if record['until'] > now:
                return 'limited', None, None
            # Constant-time comparison of fixed-size digests, including for invalid username.
            supplied = hashlib.sha256((str(username) + '\0' + str(password)).encode('utf-8', errors='surrogatepass')).digest()
            expected = hashlib.sha256(('admin\0' + self.password_getter()).encode('utf-8')).digest()
            if not hmac.compare_digest(supplied, expected):
                count = record['count'] + 1
                self.failures[addr] = {'count': count, 'last': now,
                                       'until': now + min(300, 30 * 2 ** (count - 5)) if count >= 5 else 0}
                return 'invalid', None, None
            self.failures.pop(addr, None)
            token = secrets.token_urlsafe(36)
            csrf = secrets.token_urlsafe(30)
            self.sessions[self._key(token)] = {'csrf': csrf, 'created': now, 'active': now}
            if len(self.sessions) > 128:
                oldest = min(self.sessions, key=lambda k: self.sessions[k]['created'])
                self.sessions.pop(oldest, None)
            return 'ok', token, csrf

    def get(self, raw_cookie):
        if not raw_cookie or len(raw_cookie) > 4096:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except CookieError:
            return None
        token_item = cookie.get(COOKIE_NAME)
        if token_item is None:
            return None
        token = token_item.value
        if len(token) > 200 or not token.isascii():
            return None
        with self.lock:
            now = time.monotonic()
            self._prune(now)
            session = self.sessions.get(self._key(token))
            if session is not None:
                session['active'] = now
            return session.copy() if session is not None else None

    def verify_csrf(self, session, value):
        return bool(session and isinstance(value, str) and value.isascii() and
                    hmac.compare_digest(session['csrf'], value))

    def revoke(self, raw_cookie):
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie or '')
            token_item = cookie.get(COOKIE_NAME)
            if token_item is not None:
                with self.lock:
                    self.sessions.pop(self._key(token_item.value), None)
        except (CookieError, ValueError, UnicodeError):
            pass
