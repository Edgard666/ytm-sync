"""Configuration et etat persistants (JSON dans /config)."""
import copy
import glob
import json
import os
import shutil
import time
import secrets
import threading

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
CFG_F = os.path.join(CONFIG_DIR, "config.json")
STATE_F = os.path.join(CONFIG_DIR, "state.json")
AUTH_F = os.path.join(CONFIG_DIR, "browser.json")
SECRET_F = os.path.join(CONFIG_DIR, "secret.key")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")


def _read_version():
    for p in ("/app/VERSION", os.path.join(os.path.dirname(__file__), "..", "VERSION")):
        try:
            return open(p).read().strip()
        except OSError:
            pass
    return "dev"


VERSION = _read_version()

DEFAULT_CFG = {
    "auth": {"password_hash": "", "epoch": ""},
    "navidrome": {"url": "", "user": "", "password": ""},
    "soulsync": {"url": "", "api_key": "", "profile_id": "1"},
    "nd_accounts": [],  # comptes Navidrome supplementaires {id, user, password} (proprietaires de playlists)
    "playlists": [],  # {id, source, nd_name, enabled, nd_account}
    "ignored": {},    # videoId -> {"artist", "title", "key", "at"} : titres volontairement ignores
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
        "library_cache": True,
        "star_likes": False,
        "min_bitrate": 0,
        "unstar_removed": False,
        "exclude_rules": "",
    },
    "auto_watch": {"enabled": False, "mode": "all", "prefix": "", "nd_account": "", "dismissed": []},
    "monitor": {"enabled": True, "interval_min": 5, "auto_scan": True, "resync_after": True},
    "notify": {
        "on_sync": "changes",  # always | changes | errors | never
        "on_dl_fail": True,
        "ntfy": {"enabled": False, "url": "https://ntfy.sh", "topic": "", "token": ""},
        "gotify": {"enabled": False, "url": "", "token": ""},
        "webhook": {"enabled": False, "url": "", "format": "discord"},
        "email": {"enabled": False, "host": "", "port": 587, "security": "starttls",
                  "user": "", "password": "", "from": "", "to": ""},
    },
    "security": {"behind_proxy": False, "proxy_ips": "", "idle_days": 7, "api_key": "", "api_key_hash": "", "api_key_created": None},
    "mqtt": {"enabled": False, "host": "", "port": 1883, "user": "", "password": "", "tls": False,
             "discovery_prefix": "homeassistant", "base_topic": "ytm_sync"},
    "ui": {"hide_wizard": False},
    "nd_playlists": {"enabled": True, "prefix": "YT - ", "public": False, "copy_description": True},
    "schedule": {"enabled": False, "mode": "interval", "interval_hours": 6, "daily_time": "03:00"},
    "logs_keep": 30,
}

DEFAULT_STATE = {"tracks": {}, "playlists": {}, "last_run": None, "history": [],
                 "audit": [],       # journal des corrections manuelles (avec annulation)
                 "downloaded": {},  # videoId -> morceau arrive dans Navidrome apres une demande SoulSync
                 "monitor": {}, "ytm_auth_error": None, "starred_by_app": [], "liked_count": 0,
                 "coverage": [], "exports": []}


def validate_cfg(cfg, defaults=None, path=""):
    """Remet la valeur par defaut pour tout reglage du mauvais type (fichier restaure modifie a la main…).
    Retourne la liste des reglages corriges."""
    defaults = DEFAULT_CFG if defaults is None else defaults
    fixed = []
    for k, dv in defaults.items():
        if k not in cfg:
            continue
        v, p = cfg[k], f"{path}{k}"
        if isinstance(dv, dict):
            if not isinstance(v, dict):
                cfg[k] = copy.deepcopy(dv)
                fixed.append(p)
            elif dv:                       # dictionnaire de reglages connus : on descend
                fixed += validate_cfg(v, dv, p + ".")
        elif isinstance(dv, list) and not isinstance(v, list):
            cfg[k], _ = copy.deepcopy(dv), fixed.append(p)
        elif isinstance(dv, bool) and not isinstance(v, bool):
            cfg[k], _ = dv, fixed.append(p)
        elif isinstance(dv, (int, float)) and not isinstance(dv, bool) and \
                (isinstance(v, bool) or not isinstance(v, (int, float))):
            cfg[k], _ = dv, fixed.append(p)
        elif isinstance(dv, str) and not isinstance(v, str):
            cfg[k], _ = dv, fixed.append(p)
    if not path and isinstance(cfg.get("schedule"), dict):
        import re
        if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", str(cfg["schedule"].get("daily_time", ""))):
            cfg["schedule"]["daily_time"] = "03:00"
            fixed.append("schedule.daily_time")
    if not path:   # listes d'elements : playlists, comptes, corrections, titres ignores
        pls = [x for x in cfg.get("playlists", []) if isinstance(x, dict) and isinstance(x.get("id"), str)
               and isinstance(x.get("source"), str)]
        if len(pls) != len(cfg.get("playlists", [])):
            fixed.append("playlists (entrées invalides retirées)")
        cfg["playlists"] = pls
        acc = [x for x in cfg.get("nd_accounts", []) if isinstance(x, dict) and isinstance(x.get("id"), str)
               and isinstance(x.get("user"), str)]
        if len(acc) != len(cfg.get("nd_accounts", [])):
            fixed.append("nd_accounts (entrées invalides retirées)")
        cfg["nd_accounts"] = acc
        for k in ("ignored", "overrides"):
            bad = [vid for vid, x in cfg.get(k, {}).items() if not isinstance(x, dict)]
            for vid in bad:
                del cfg[k][vid]
            if bad:
                fixed.append(f"{k} ({len(bad)} entrée(s) invalide(s) retirée(s))")
    return fixed


def _merge(data, defaults):
    out = copy.deepcopy(defaults)
    for k, v in (data or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(v, out[k])
        else:
            out[k] = v
    return out


def _atomic_write(path, obj):
    """Ecriture sure : fichier temporaire force sur le disque (fsync), puis remplacement atomique.
    Une copie .bak de la version precedente est gardee (au plus toutes les 10 min)."""
    for attempt in range(3):          # un autre fil peut modifier l'objet pendant la serialisation
        try:
            data = json.dumps(obj, indent=1, ensure_ascii=False)
            break
        except RuntimeError:
            time.sleep(0.05)
    else:
        data = json.dumps(copy.deepcopy(obj), indent=1, ensure_ascii=False)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)  # contient des mots de passe : lisible par l'application seulement
    bak = path + ".bak"
    try:
        if os.path.exists(path) and (not os.path.exists(bak) or time.time() - os.path.getmtime(bak) > 600):
            shutil.copy2(path, bak)
            os.chmod(bak, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:  # rend le renommage durable lui aussi
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        os.fsync(dfd)
        os.close(dfd)
    except OSError:
        pass


def secure_file(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Store:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.lock = threading.RLock()
        self.recovered = []  # messages affiches si un fichier abime a ete recupere
        self.cfg = _merge(self._load(CFG_F), DEFAULT_CFG)
        self.state = _merge(self._load(STATE_F), DEFAULT_STATE)

    def _load(self, path):
        """Charge un fichier JSON. S'il est abime : il est mis de cote (jamais ecrase) et la derniere
        copie saine (.bak, puis sauvegarde quotidienne) est utilisee a la place."""
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            raise ValueError("contenu inattendu")
        except (OSError, ValueError) as ex:
            name = os.path.basename(path)
            bad = f"{path}.abime-{time.strftime('%Y%m%d-%H%M%S')}"
            try:
                os.replace(path, bad)
            except OSError:
                bad = path
            for cand in [path + ".bak"] + sorted(glob.glob(os.path.join(BACKUP_DIR, name.replace(".json", "-*.json"))),
                                                 reverse=True):
                try:
                    with open(cand, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        msg = (f"{name} était abîmé ({ex}) : récupéré depuis {os.path.basename(cand)}. "
                               f"Le fichier abîmé est conservé : {os.path.basename(bad)}")
                        self.recovered.append(msg)
                        print("!!", msg, flush=True)
                        return data
                except (OSError, ValueError):
                    continue
            msg = (f"{name} était abîmé ({ex}) et aucune copie saine n'a été trouvée : valeurs par défaut. "
                   f"Le fichier abîmé est conservé : {os.path.basename(bad)}")
            self.recovered.append(msg)
            print("!!", msg, flush=True)
            return {}

    def daily_backup(self, keep=14):
        """Copie datee de config.json et state.json dans /config/backups (garde les `keep` derniers jours)."""
        os.makedirs(BACKUP_DIR, exist_ok=True)
        day = time.strftime("%Y%m%d")
        done = False
        for src, obj in ((CFG_F, self.cfg), (STATE_F, self.state)):
            dst = os.path.join(BACKUP_DIR, os.path.basename(src).replace(".json", f"-{day}.json"))
            if not os.path.exists(dst):
                with self.lock:
                    _atomic_write(dst, obj)
                done = True
            files = sorted(glob.glob(os.path.join(BACKUP_DIR, os.path.basename(src).replace(".json", "-*.json"))))
            for old in files[:-keep]:
                try:
                    os.remove(old)
                except OSError:
                    pass
        return done

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
        secure_file(SECRET_F)
        return open(SECRET_F).read().strip()
