"""Logique metier partagee par les routes : journal des corrections, titres ignores, menage, doublons, sauvegardes, etat."""
import collections
import datetime as dt
import os
import secrets
import time

from .clients import Navidrome, soulsync_from
from . import core
from .core import BACKUP_RE, ND_EMPTY, _key_hash, new_epoch, runner, store
from .store import VERSION
from .sync import next_run_ts


# ------------------------------------------------------------------ pages
def setup_steps():
    c, h = store.cfg, store.state.get("history", [])
    return [
        ("Connecter Navidrome", bool(c["navidrome"]["url"] and c["navidrome"]["user"]), "/settings"),
        ("Connecter SoulSync", bool(c["soulsync"]["url"] and c["soulsync"]["api_key"]), "/settings"),
        ("Ajouter au moins une playlist YouTube", bool(c["playlists"]), "/playlists"),
        ("Lancer une simulation et vérifier les correspondances", any(x.get("dry") for x in h), "/"),
        ("Passer en mode réel (décocher « simulation » dans Paramètres)", not c["sync"]["dry_run"], "/settings"),
        ("Activer la synchro automatique", c["schedule"]["enabled"], "/settings"),
    ]


def _name_taken(name, account, self_id=None):
    """Nom Navidrome deja vise par une autre playlist du meme compte (noms explicites et derives)."""
    name = (name or "").strip().lower()
    if not name:
        return False
    for p in store.cfg["playlists"]:
        if p["id"] == self_id or p.get("nd_account", "") != (account or ""):
            continue
        other = (p.get("nd_name") or store.state["playlists"].get(p["id"], {}).get("nd_name") or "").strip().lower()
        if other == name:
            return True
    return False


def csv_cell(v):
    """Neutralise l'injection de formules (titre YouTube commencant par =, +, -, @…)."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def csv_row(w, cells):
    w.writerow([csv_cell(c) for c in cells])


def _nd():
    c = store.cfg["navidrome"]
    return Navidrome(c["url"], c["user"], c["password"])


def _nd_fields(song):
    return dict(nd_id=song["id"], nd_artist=song.get("artist"), nd_title=song.get("title"),
                nd_album=song.get("album"), nd_duration=song.get("duration"), nd_path=song.get("path"))


def list_backups():
    from .store import BACKUP_DIR
    if not os.path.isdir(BACKUP_DIR):
        return []
    days = {}
    for f in os.listdir(BACKUP_DIR):
        if BACKUP_RE.match(f):
            kind, day = f[:-5].split("-")
            days.setdefault(day, {})[kind] = os.path.getsize(os.path.join(BACKUP_DIR, f))
    return [{"day": d, "files": v} for d, v in sorted(days.items(), reverse=True)]


def _label(vid):
    e = store.state.get("tracks", {}).get(vid) or {}
    return f"{e.get('artist') or '?'} — {e.get('title') or vid}"


def audit(action, vids, detail, undo=None):
    with store.lock:
        log = store.state.setdefault("audit", [])
        log.insert(0, {"id": secrets.token_hex(5), "at": time.time(), "action": action, "vids": list(vids)[:500],
                       "detail": detail, "undo": undo, "undone": False})
        del log[500:]
        store.save_state()


def set_ignored(vids, on, record=True):
    """Ignore / n'ignore plus des titres (et les autres videos du meme morceau).
    Retourne le nombre de titres retires de la wishlist SoulSync."""
    to_remove = []
    if record:
        names = ", ".join(_label(v) for v in vids[:3]) + (f" et {len(vids) - 3} autre(s)" if len(vids) > 3 else "")
        audit("ignore" if on else "unignore", vids, names,
              undo={"type": "unignore" if on else "ignore", "vids": list(vids)})
    with store.lock:
        ign = store.cfg.setdefault("ignored", {})
        tracks = store.state["tracks"]
        for vid in vids:
            e = tracks.get(vid)
            key = (e or {}).get("key")
            if on:
                ign[vid] = {"artist": (e or {}).get("artist"), "title": (e or {}).get("title"), "key": key,
                            "at": time.time()}
                for x in tracks.values():  # meme morceau sous une autre video : ignore aussi
                    if x["videoId"] == vid or (key and x.get("key") == key):
                        if x.get("status") == "requested":
                            to_remove.append(x["videoId"])
                        x.update(status="ignored", warn=None, error=None, requested_at=None,
                                 dl_status=None, dl_progress=None, dl_error=None, **ND_EMPTY,
                                 ignore_reason="ignoré manuellement" if x["videoId"] == vid
                                 else "même morceau qu'un titre ignoré")
            else:
                ign.pop(vid, None)
                for x in tracks.values():
                    if x["videoId"] == vid or (key and x.get("key") == key and x.get("status") == "ignored"):
                        x.update(status="missing", ignore_reason=None, **ND_EMPTY)  # reevalue a la prochaine synchro
        store.save_cfg()
        store.save_state()
    removed = 0
    ss = soulsync_from(store.cfg)
    if ss:
        for v in to_remove:  # retire de la wishlist SoulSync ce qui avait deja ete demande
            try:
                removed += ss.delete_wishlist(f"ytm_{v}") < 300
            except Exception:
                pass
    return removed


# ------------------------------------------------------------------ menage (titres telecharges devenus orphelins)
def orphans():
    tracks = store.state.get("tracks", {})
    live_keys = {e.get("key") for e in tracks.values() if e.get("status") != "ignored"}
    out = []
    for vid, d in store.state.get("downloaded", {}).items():
        e = tracks.get(vid)
        if e and e.get("status") != "ignored":
            continue
        if not e and d.get("key") in live_keys:  # meme morceau toujours present via une autre video
            continue
        out.append(dict(d, videoId=vid, reason="ignoré depuis" if e else "retiré de toutes les playlists"))
    return sorted(out, key=lambda x: ((x.get("artist") or "").lower(), x.get("title") or ""))


def quality_label(f):
    if not f.get("suffix") and not f.get("bitRate"):
        return "qualité inconnue"
    return f"{(f.get('suffix') or '?').upper()}" + (f" {f['bitRate']} kbps" if f.get("bitRate") else "")


def find_duplicates():
    """Morceaux presents en plusieurs exemplaires dans Navidrome (meme artiste, titre et version)."""
    from .engine import get_library, quality_rank, version_tags
    lib = get_library(_nd())
    groups = collections.defaultdict(list)
    for x in lib.songs:
        if x.get("_nt") and x.get("_na"):
            # la version (live, remix…) se lit aussi dans l'album : « Live at Wembley » n'est pas un doublon du studio
            groups[(x["_na"], x["_nt"], ",".join(sorted(version_tags(x.get("title"), x.get("album")))))].append(x)
    # meme duree (a 6 s pres) : sinon ce sont des enregistrements differents (deux concerts, version longue…)
    clusters = []
    for files in groups.values():
        if len(files) < 2:
            continue
        cur = []
        for f in sorted(files, key=lambda f: f.get("duration") or 0):
            if cur and f.get("duration") and cur[-1].get("duration") and f["duration"] - cur[-1]["duration"] > 6:
                clusters.append(cur)
                cur = []
            cur.append(f)
        clusters.append(cur)
    out = []
    for files in clusters:
        if len(files) < 2:
            continue
        files = sorted(files, key=lambda f: -quality_rank(f))
        out.append({"artist": files[0].get("artist"), "title": files[0].get("title"),
                    "files": [{"id": f["id"], "album": f.get("album"), "path": f.get("path"),
                               "quality": quality_label(f),
                               "duration": f.get("duration"), "best": i == 0} for i, f in enumerate(files)]})
    out.sort(key=lambda g: ((g["artist"] or "").lower(), (g["title"] or "").lower()))
    return out, len(lib.songs)


def migrate_api_key():
    """Versions < 3.4 : la cle etait stockee en clair. On ne garde plus que son empreinte (Home Assistant
    continue de fonctionner avec la meme cle)."""
    sec = store.cfg["security"]
    if sec.get("api_key"):
        sec["api_key_hash"] = _key_hash(sec["api_key"])
        sec["api_key_created"] = sec.get("api_key_created") or time.time()
        sec["api_key"] = ""
        store.save_cfg()


def status_counts():
    c = collections.Counter(e.get("status") for e in store.state.get("tracks", {}).values())
    d = collections.Counter(e.get("dl_status") for e in store.state.get("tracks", {}).values()
                            if e.get("status") == "requested")
    return {"present": c["present"], "missing": c["missing"], "requested": c["requested"],
            "ignored": c["ignored"], "error": c["error"], "dl_failed": d["failed"], "dl_queued": d["queued"], "dl_done": d["completed"] + d["gone"],
            "dl_active": d["pending"] + d["searching"] + d["downloading"], "unmatched": d["unmatched"],
            "low_quality": sum(1 for e in store.state.get("tracks", {}).values()
                               if e.get("status") == "present" and e.get("low_quality")),
            "orphans": len(orphans())}


def status_payload():
    """Etat expose a Home Assistant (API REST et MQTT)."""
    lr = store.state.get("last_run") or {}
    nxt = next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at)
    cov = store.state.get("coverage") or []
    return dict(ok=True, version=VERSION, running=runner.running, progress=runner.progress,
                state="en cours" if runner.running else ("erreur" if lr.get("status") == "erreur" else "au repos"),
                last_run={k: lr.get(k) for k in ("start", "end", "status", "trigger", "total", "present",
                                                 "missing", "sent", "errors", "dry")},
                last_run_iso=dt.datetime.fromtimestamp(lr["start"]).astimezone().isoformat() if lr.get("start") else None,
                next_run_iso=dt.datetime.fromtimestamp(nxt).astimezone().isoformat() if nxt else None,
                coverage=round(cov[-1]["present"] / cov[-1]["total"] * 100, 1) if cov and cov[-1]["total"] else None,
                ytm_session_ok=not store.state.get("ytm_auth_error"),
                counts=status_counts())


def startup_housekeeping():
    """Au demarrage : epoch de session, code d'installation, nettoyage des secrets des anciens journaux."""
    from .engine import redact
    from .store import LOG_DIR
    if not store.cfg["auth"].get("epoch"):
        new_epoch()
        store.save_cfg()
    if not store.cfg["auth"]["password_hash"]:
        raw = secrets.token_hex(4).upper()
        core.SETUP_CODE = f"{raw[:4]}-{raw[4:]}"
        print("=" * 60, f"\n  Premier démarrage : code d'installation = {core.SETUP_CODE}\n"
              "  (à saisir dans l'interface web pour créer le mot de passe)\n" + "=" * 60, flush=True)
    # les versions < 3.2 pouvaient ecrire le jeton Navidrome dans les journaux : on le masque
    cleaned = 0
    for f in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, f)
        try:
            txt = open(path, encoding="utf-8").read()
            red = redact(txt)
            if red != txt:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(red)
                cleaned += 1
        except OSError:
            pass
    changed = False
    for h in store.state.get("history", []) + [store.state.get("last_run") or {}]:
        if h.get("message") and redact(h["message"]) != h["message"]:
            h["message"], changed = redact(h["message"]), True
    for e in store.state.get("tracks", {}).values():
        if e.get("error") and redact(e["error"]) != e["error"]:
            e["error"], changed = redact(e["error"]), True
    if changed:
        store.save_state()
    if cleaned:
        print(f"Secrets masqués dans {cleaned} ancien(s) journal(aux)", flush=True)
