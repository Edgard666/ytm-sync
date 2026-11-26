"""Private connection overrides. Docker supplies defaults for omitted fields."""
import json
import logging
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from urllib.parse import urlsplit

LOCK = RLock()
FIELDS = {'url': 'QBIT_URL', 'username': 'QBIT_USERNAME', 'password': 'QBIT_PASSWORD'}


def defaults():
    return {key: os.getenv(env, '') for key, env in FIELDS.items()}


def effective(data):
    with LOCK:
        path = Path(data) / 'qbit-connection.json'
        saved = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
                # Keep only known string fields: a damaged file must not stop the controller.
                saved = {k: v for k, v in raw.items() if k in FIELDS and isinstance(v, str)} \
                    if isinstance(raw, dict) else {}
            except (ValueError, OSError):
                logging.getLogger('joal-smart').error(
                    'qbit-connection.json illisible : valeurs Docker utilisées')
        return {**defaults(), **saved}


def public(data):
    config = effective(data)
    return {'url': config['url'], 'username': config['username'], 'hasPassword': bool(config['password'])}


def save(data, body):
    path = Path(data) / 'qbit-connection.json'
    with LOCK:
        if body == {'useDocker': True}:
            path.unlink(missing_ok=True)
            return
        if not isinstance(body, dict) or not {'url', 'username'} <= body.keys() <= FIELDS.keys():
            raise ValueError('Champs de connexion invalides')
        if not all(isinstance(v, str) and len(v) <= 2048 for v in body.values()):
            raise ValueError('Valeur de connexion invalide')
        url = body['url'].strip().rstrip('/')
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname and parsed.username is None
                     and not parsed.query and not parsed.fragment and (parsed.port is None or parsed.port > 0)
                     and not any(c.isspace() or ord(c) < 32 for c in url))
        except ValueError:
            valid = False
        if url and not valid:
            raise ValueError('URL qBittorrent HTTP(S) invalide')
        old = effective(data)
        # A new endpoint must not silently inherit credentials for the old endpoint.
        password = body.get('password', old['password'] if url == old['url'].rstrip('/') else '')
        config = dict(url=url, username=body['username'].strip(), password=password)
        env = defaults()
        saved = {k: v for k, v in config.items() if v != env[k]}
        # Pin the password (even when empty) when targeting a different server.
        if url != env['url'].rstrip('/'):
            saved['password'] = password
        with NamedTemporaryFile(mode='w', dir=data, delete=False, encoding='utf-8') as f:
            try:
                json.dump(saved, f)
                f.flush()
                os.fsync(f.fileno())
                os.replace(f.name, path)
            finally:
                Path(f.name).unlink(missing_ok=True)
