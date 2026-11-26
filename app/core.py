"""JOAL companion - policy evaluation and safe torrent metadata filtering.

Only the trackers that satisfy a policy are included in an exported torrent.
The original `info` bencoded slice is preserved byte-for-byte to keep its hashes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


RULE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
DAY_NAMES = {1: "lun", 2: "mar", 3: "mer", 4: "jeu", 5: "ven", 6: "sam", 7: "dim"}


def _minutes(value: str) -> int:
    match = HHMM.fullmatch(value)
    return int(match.group(1)) * 60 + int(match.group(2))


def validate_schedule(rule: dict):
    """Optional time window: {"days": [1..7] (1 = lundi), "start": "HH:MM", "end": "HH:MM"}.

    start > end means an overnight window (22:00-07:00); `days` names the day
    on which the window *starts*. Missing or null schedule = always active.
    """
    schedule = rule.get("schedule")
    if schedule is None:
        return
    rid = rule.get("id")
    if not isinstance(schedule, dict) or set(schedule) != {"days", "start", "end"}:
        raise ConfigError(f"Règle {rid}: schedule doit contenir days, start et end")
    days = schedule["days"]
    if (not isinstance(days, list) or not days or len(set(days)) != len(days)
            or not all(type(d) is int and 1 <= d <= 7 for d in days)):
        raise ConfigError(f"Règle {rid}: jours de la plage horaire invalides (1 = lundi … 7 = dimanche)")
    for key in ("start", "end"):
        if not isinstance(schedule[key], str) or not HHMM.fullmatch(schedule[key]):
            raise ConfigError(f"Règle {rid}: heure {key} invalide (format HH:MM)")
    if schedule["start"] == schedule["end"]:
        raise ConfigError(f"Règle {rid}: début et fin de plage identiques (retirer la plage pour 24 h/24)")
    schedule["days"] = sorted(days)


def rule_active_now(rule: dict, now: dt.datetime | None = None) -> bool:
    """True when the rule has no schedule or the local time is inside it."""
    schedule = rule.get("schedule")
    if not schedule:
        return True
    now = now or dt.datetime.now()
    start, end = _minutes(schedule["start"]), _minutes(schedule["end"])
    current, day = now.hour * 60 + now.minute, now.isoweekday()
    days = set(schedule["days"])
    if start < end:
        return day in days and start <= current < end
    if current >= start:                       # evening part of an overnight window
        return day in days
    if current < end:                          # morning part: window started yesterday
        return (7 if day == 1 else day - 1) in days
    return False


def schedule_label(rule: dict) -> str:
    schedule = rule.get("schedule")
    if not schedule:
        return "24 h/24"
    days = schedule["days"]
    names = "tous les jours" if len(days) == 7 else ", ".join(DAY_NAMES[d] for d in days)
    return f"{names} {schedule['start']}–{schedule['end']}"


def domain(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in ("https", "http", "udp"):
            return None
        return (parsed.hostname or "").lower().rstrip(".") or None
    except ValueError:
        return None


def domain_matches(host: str | None, pattern: str) -> bool:
    if host is None:
        return False
    pattern = pattern.lower().rstrip(".")
    return host == pattern or host.endswith("." + pattern)


STOPPED_COMPLETE_STATES = frozenset({"stoppedUP", "pausedUP"})


def is_complete_stopped(torrent: dict) -> bool:
    """Fail closed. stoppedUP is qBittorrent 5.x; pausedUP is the older name."""
    try:
        progress = float(torrent.get("progress", 0))
    except (ValueError, TypeError):
        return False
    if not math.isfinite(progress) or progress < 1.0:
        return False
    remaining = torrent.get("amount_left")
    if remaining is not None and (type(remaining) is not int or remaining != 0):
        return False
    return torrent.get("state") in STOPPED_COMPLETE_STATES


def recorded_ratio(torrent: dict) -> float | None:
    """Only qBittorrent's verified local ratio. Never interprets JOAL counters."""
    value = torrent.get("ratio")
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def watch_categories(config):
    return tuple(dict.fromkeys(name.strip() for name in config.get("qbitCategory", "").splitlines()
                               if name.strip()))


def validate(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ConfigError("Le fichier doit contenir un objet JSON")
    if type(config.get("dryRun")) is not bool:
        raise ConfigError("dryRun doit être true ou false")
    config.setdefault("completedTag", "JoalCompleted")
    # Check container types first: later checks dereference these objects.
    for section in ("handoff", "swarm", "cleanup"):
        if section in config and not isinstance(config[section], dict):
            raise ConfigError(f"{section} doit être un objet")
    config.setdefault("handoff", {"enabled": False, "cooldownMinutes": 60, "confirmations": 2})
    handoff = config["handoff"]
    if not isinstance(handoff, dict):
        raise ConfigError("handoff doit être un objet")
    handoff.setdefault("enabled", False)
    handoff.setdefault("cooldownMinutes", 60)
    handoff.setdefault("confirmations", 2)
    # One fresh valid tracker observation may confirm a large surplus.
    # Existing installations inherit a five-leecher margin without migration.
    handoff.setdefault("fastTrackMargin", 5)
    if type(handoff["enabled"]) is not bool:
        raise ConfigError("handoff.enabled doit être booléen")
    if type(handoff["cooldownMinutes"]) is not int or not 60 <= handoff["cooldownMinutes"] <= 10080:
        raise ConfigError("handoff.cooldownMinutes doit être entre 60 et 10080")
    if type(handoff["confirmations"]) is not int or not 2 <= handoff["confirmations"] <= 5:
        raise ConfigError("handoff.confirmations doit être entre 2 et 5")
    if type(handoff["fastTrackMargin"]) is not int or not 1 <= handoff["fastTrackMargin"] <= 1000:
        raise ConfigError("handoff.fastTrackMargin doit être entre 1 et 1000")
    if handoff["enabled"] and not config.get("swarm", {}).get("enabled", True):
        raise ConfigError("Activer les relevés autonomes avant le basculement")
    config.setdefault("guardSeconds", 5)
    config.setdefault("cleanup", {"enabled": False, "afterDays": 3, "deleteData": False, "scrapeEveryMinutes": 60})
    config.setdefault("swarm", {"enabled": True, "intervalMinutes": 15, "maxAgeMinutes": 45})
    swarm = config["swarm"]
    if not isinstance(swarm, dict):
        raise ConfigError("swarm doit être un objet")
    swarm.setdefault("enabled", True)
    swarm.setdefault("intervalMinutes", 15)
    swarm.setdefault("maxAgeMinutes", 45)
    if type(swarm["enabled"]) is not bool:
        raise ConfigError("swarm.enabled doit être booléen")
    for key, lo, hi in (("intervalMinutes", 15, 1440), ("maxAgeMinutes", 15, 2880)):
        if type(swarm[key]) is not int or not lo <= swarm[key] <= hi:
            raise ConfigError(f"swarm.{key} doit être entre {lo} et {hi}")
    if swarm["maxAgeMinutes"] < swarm["intervalMinutes"]:
        raise ConfigError("swarm.maxAgeMinutes doit être >= intervalMinutes")
    cleanup = config["cleanup"]
    if not isinstance(cleanup, dict):
        raise ConfigError("cleanup doit être un objet")
    cleanup.setdefault("enabled", False)
    cleanup.setdefault("afterDays", 3)
    cleanup.setdefault("deleteData", False)
    cleanup.setdefault("scrapeEveryMinutes", 60)
    if type(cleanup["enabled"]) is not bool or type(cleanup["deleteData"]) is not bool:
        raise ConfigError("cleanup.enabled et cleanup.deleteData doivent être booléens")
    if type(cleanup["afterDays"]) is not int or not 3 <= cleanup["afterDays"] <= 365:
        raise ConfigError("cleanup.afterDays doit être compris entre 3 et 365 jours")
    if type(cleanup["scrapeEveryMinutes"]) is not int or not 30 <= cleanup["scrapeEveryMinutes"] <= 1440:
        raise ConfigError("cleanup.scrapeEveryMinutes doit être compris entre 30 et 1440 minutes")
    if cleanup["deleteData"] and not cleanup["enabled"]:
        raise ConfigError("Activer cleanup.enabled avant cleanup.deleteData")
    mode = config.setdefault("qbitFilterMode", "tag")
    if mode not in ("tag", "category", "either"):
        raise ConfigError("Choisir le suivi par tag ou par catégorie")
    category = config.setdefault("qbitCategory", "")
    if not isinstance(category, str):
        raise ConfigError("Renseigner un nom de catégorie par ligne")
    categories = watch_categories(config)
    if (mode != "tag" and not categories) or any(len(name) > 255 for name in categories):
        raise ConfigError("Renseigner un nom de catégorie par ligne (255 caractères maximum par nom)")
    config["qbitCategory"] = "\n".join(categories)
    config.setdefault("qbitTag", "joal")
    completed = config["completedTag"]
    if not isinstance(completed, str) or not completed or len(completed) > 50 or "," in completed:
        raise ConfigError("completedTag doit être un tag simple de 1 à 50 caractères")
    if mode != "category" and completed.casefold() == str(config.get("qbitTag", "joal")).casefold():
        raise ConfigError("completedTag doit être différent de qbitTag")
    if type(config["guardSeconds"]) is not int or not 3 <= config["guardSeconds"] <= 60:
        raise ConfigError("guardSeconds doit être compris entre 3 et 60")
    tag = config.get("qbitTag", "joal")
    if not isinstance(tag, str) or (mode != "category" and not tag.strip()) or len(tag) > 50:
        raise ConfigError("qbitTag est obligatoire (maximum 50 caractères)")
    # Backward compatible: maxCandidates now limits the number of *selected* torrents,
    # never the number of tagged torrents that may be observed.
    for key, minv, maxv in (("refreshSeconds", 60, 3600), ("maxCandidates", 1, 1000)):
        val = config.get(key)
        if type(val) is not int or not minv <= val <= maxv:
            raise ConfigError(f"{key} doit être compris entre {minv} et {maxv}")
    # Explicit assignment is a per-infohash exception. Missing means automatic.
    # Do not mutate trackerDomains: this setting must affect only one torrent.
    config.setdefault("torrentRuleOverrides", {})
    assignments = config["torrentRuleOverrides"]
    if not isinstance(assignments, dict) or len(assignments) > 500:
        raise ConfigError("torrentRuleOverrides doit être un objet (500 entrées max)")
    for infohash, rule_id in assignments.items():
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-f]{40}", infohash):
            raise ConfigError("torrentRuleOverrides : infohash v1 invalide")
        if not isinstance(rule_id, str) or not RULE_ID.fullmatch(rule_id):
            raise ConfigError("torrentRuleOverrides : identifiant de règle invalide")
    rules = config.get("rules")
    if not isinstance(rules, list) or len(rules) > 20:
        raise ConfigError("rules doit contenir au maximum 20 règles")
    ids = set()
    for r in rules:
        if not isinstance(r, dict) or not RULE_ID.fullmatch(str(r.get("id", ""))):
            raise ConfigError("Identifiant de règle invalide")
        if r["id"] in ids:
            raise ConfigError("Identifiant de règle en double")
        ids.add(r["id"])
        if type(r.get("enabled")) is not bool:
            raise ConfigError(f"Règle {r['id']}: enabled doit être booléen")
        domains = r.get("trackerDomains")
        if not isinstance(domains, list) or len(domains) > 20:
            raise ConfigError(f"Règle {r['id']}: trackerDomains est requis")
        for value in domains:
            if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9.-]{3,255}", value) or value.startswith(".") or ".." in value:
                raise ConfigError("Fournir des domaines de tracker, pas des URL/passkeys")
            # Several policies may intentionally cover the same tracker. Their
            # priority is determined per torrent, not by configuration order.
        # Optional per-torrent override; a rule-wide threshold remains the default.
        r.setdefault("minLeechersByHash", {})
        overrides = r["minLeechersByHash"]
        if not isinstance(overrides, dict) or len(overrides) > 500:
            raise ConfigError(f"Règle {r['id']}: minLeechersByHash doit être un objet (500 entrées max)")
        for hash_, threshold in overrides.items():
            if not isinstance(hash_, str) or not re.fullmatch(r"[0-9a-f]{40}", hash_):
                raise ConfigError(f"Règle {r['id']}: infohash d'exception invalide")
            if type(threshold) is not int or not 0 <= threshold <= 100000:
                raise ConfigError(f"Règle {r['id']}: seuil individuel invalide pour {hash_[:12]}")
        r.setdefault("targetRatio", 0.0)
        ratio = r["targetRatio"]
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio < 0 or ratio > 100:
            raise ConfigError(f"Règle {r['id']}: targetRatio doit être compris entre 0 et 100")
        for key, lo, hi in (("minLeechers", 0, 100000), ("minUploadRate", 0, 100000),
                            ("maxUploadRate", 0, 100000), ("simultaneousSeed", 1, 250)):
            val = r.get(key)
            if type(val) is not int or not lo <= val <= hi:
                raise ConfigError(f"Règle {r['id']}: {key} doit être entre {lo} et {hi}")
        client = r.get("client")
        if client is not None and (not isinstance(client, str) or
                not re.fullmatch(r"[a-zA-Z0-9._-]{3,120}\.client", client)):
            raise ConfigError(f"Règle {r['id']}: nom de client JOAL invalide")
        if r["minUploadRate"] == 0 and r["maxUploadRate"] != 0:
            raise ConfigError("Pour le seed sans upload, régler les deux vitesses à 0")
        if r["minUploadRate"] > r["maxUploadRate"]:
            raise ConfigError(f"Règle {r['id']}: minUploadRate dépasse maxUploadRate")
        validate_schedule(r)
    return config


def rules_missing_client(config: dict) -> list[str]:
    """Enabled upload rules that cannot launch JOAL (no .client profile).

    Checked when saving from the WebUI only, so that an existing policy.json
    keeps loading after an upgrade.
    """
    return [r["id"] for r in config.get("rules", [])
            if r.get("enabled") and r.get("maxUploadRate", 0) > 0 and not r.get("client")]


def rule_preference(rule: dict, infohash: str) -> tuple:
    """Sort enabled matching rules from least restrictive to most restrictive.

    First use the effective leecher threshold (including per-torrent overrides).
    For equal thresholds, unlimited or higher ratio targets take precedence;
    finally prefer higher per-rule capacity and use a stable ID tie-breaker.
    Speeds are *not* used to determine permissiveness.
    """
    threshold = rule.get("minLeechersByHash", {}).get(infohash.lower(), rule["minLeechers"])
    target = rule.get("targetRatio", 0) or float("inf")
    return (threshold, -target, -rule["simultaneousSeed"], rule["id"])


def allowed_tracker_urls(trackers: list[dict], rule: dict, threshold: int | None = None) -> list[str]:
    """Fail closed for tracker status, invalid leecher counts, or unsupported protocols."""
    found = []
    if threshold is None:
        threshold = rule["minLeechers"]
    for item in trackers:
        url = item.get("url", "")
        host = domain(url) if isinstance(url, str) else None
        leecher_count = item.get("num_leeches")
        if (host and type(leecher_count) is int and leecher_count >= threshold
                and item.get("status") == 2
                and any(domain_matches(host, d) for d in rule["trackerDomains"])):
            if url not in found:
                found.append(url)
    return found


def decode_bencode(src: bytes, pos: int = 0):
    if pos >= len(src):
        raise ValueError("Métadonnées bencode tronquées")
    ch = src[pos:pos+1]
    if ch == b"i":
        end = src.index(b"e", pos+1)
        raw = src[pos+1:end]
        if not raw or (raw.startswith(b"0") and len(raw) > 1) or raw.startswith(b"-0"):
            raise ValueError("Entier bencode invalide")
        return int(raw), end+1
    if ch == b"l":
        values = []
        pos += 1
        while src[pos:pos+1] != b"e":
            value, pos = decode_bencode(src, pos)
            values.append(value)
        return values, pos+1
    if ch == b"d":
        values = {}
        pos += 1
        while src[pos:pos+1] != b"e":
            key, pos = decode_bencode(src, pos)
            if not isinstance(key, bytes):
                raise ValueError("Clé non binaire")
            value, pos = decode_bencode(src, pos)
            values[key] = value
        return values, pos+1
    if b"0" <= ch <= b"9":
        colon = src.index(b":", pos)
        size = int(src[pos:colon])
        start = colon+1
        end = start+size
        if end > len(src):
            raise ValueError("Chaîne bencode tronquée")
        return src[start:end], end
    raise ValueError("Valeur bencode invalide")


def encode_bencode(value) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if type(value) is int:
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(encode_bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(encode_bencode(k)+encode_bencode(v) for k,v in sorted(value.items())) + b"e"
    raise TypeError("Type bencode non pris en charge")


def get_info_slice(data: bytes) -> bytes:
    """Find the original raw top-level info dictionary without round-tripping it."""
    if data[:1] != b"d":
        raise ValueError("Le .torrent n'est pas un dictionnaire bencode")
    idx = 1
    while data[idx:idx+1] != b"e":
        key, idx = decode_bencode(data, idx)
        start = idx
        _, idx = decode_bencode(data, idx)
        if key == b"info":
            return data[start:idx]
    raise ValueError("Dictionnaire info absent")


def filter_torrent(data: bytes, trackers: list[str], expected_hash: str | None = None) -> bytes:
    """Keep only selected HTTP(S) trackers while preserving the exact info hash."""
    if not trackers or any(domain(u) is None for u in trackers):
        raise ValueError("Aucun tracker HTTP(S) autorisé")
    root, end = decode_bencode(data)
    if end != len(data) or not isinstance(root, dict):
        raise ValueError(".torrent incorrect")
    info_bytes = get_info_slice(data)
    if expected_hash:
        h = hashlib.sha1(info_bytes).hexdigest() if len(expected_hash) == 40 else hashlib.sha256(info_bytes).hexdigest()
        if h.lower() != expected_hash.lower():
            raise ValueError("Infohash exporté différent de qBittorrent")
    root[b"announce"] = trackers[0].encode("utf-8")
    root[b"announce-list"] = [[u.encode("utf-8")] for u in trackers]
    output = b"d"
    for key, value in sorted(root.items()):
        output += encode_bencode(key) + (info_bytes if key == b"info" else encode_bencode(value))
    output += b"e"
    if get_info_slice(output) != info_bytes:
        raise ValueError("L'infohash a changé")
    return output


def summary(config):
    return json.dumps(config, indent=2, ensure_ascii=False)
