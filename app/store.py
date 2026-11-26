"""Configuration et etat persistants (JSON dans /config)."""
import copy
import json
import os
import secrets
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CFG_F = os.path.join(CONFIG_DIR, "config.json")
STATE_F = os.path.join(CONFIG_DIR, "state.json")
AUTH_F = os.path.join(CONFIG_DIR, "browser.json")
SECRET_F = os.path.join(CONFIG_DIR, "secret.key")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")


def _read_version():
    for p in ("/app/VERSION", os.path.join(os.path.dirname(__file__), "..", "VERSION")):
        try:
            return open(p).read().strip()
        except OSError:
            pass
    return "dev"


VERSION = _read_version()

DEFAULT_CFG = {
    "auth": {"password_hash": ""},
    "navidrome": {"url": "", "user": "", "password": ""},
    "soulsync": {"url": "", "api_key": "", "profile_id": "1"},
    "playlists": [],  # {id, source, nd_name, enabled}
    "ignored": {},    # videoId -> {"artist", "title", "at"} : titres volontairement ignores
    "overrides": {},  # videoId -> {"nd_id": lien manuel, "blocked": [ids Navidrome refuses]}
    "sync": {
        "dry_run": True,
        "trigger_process": True,
        "scan_before": False,
        "title_threshold": 0.85,
        "artist_threshold": 0.60,
        "re_request_days": 7,
        "duration_tolerance": 15,
        "strict_versions": True,
    },
    "nd_playlists": {"enabled": True, "prefix": "YT - ", "public": False},
    "schedule": {"enabled": False, "mode": "interval", "interval_hours": 6, "daily_time": "03:00"},
    "logs_keep": 30,
}

DEFAULT_STATE = {"tracks": {}, "playlists": {}, "last_run": None, "history": []}


def _merge(data, defaults):
    out = copy.deepcopy(defaults)
    for k, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(v, out[k])
        else:
            out[k] = v
    return out


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


class Store:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.lock = threading.RLock()
        self.cfg = _merge(self._load(CFG_F), DEFAULT_CFG)
        self.state = _merge(self._load(STATE_F), DEFAULT_STATE)

    @staticmethod
    def _load(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_cfg(self):
        with self.lock:
            _atomic_write(CFG_F, self.cfg)

    def save_state(self):
        with self.lock:
            _atomic_write(STATE_F, self.state)

    @staticmethod
    def secret_key():
        if not os.path.exists(SECRET_F):
            with open(SECRET_F, "w") as f:
                f.write(secrets.token_hex(32))
        return open(SECRET_F).read().strip()
