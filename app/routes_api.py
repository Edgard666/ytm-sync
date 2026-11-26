"""Routes : API JSON de l'interface (etat, synchro, tests de connexion, YouTube, SoulSync, Navidrome, export, notifications)."""
import os
import threading
import uuid

from flask import jsonify, request
from . import notify
from .clients import Navidrome, SoulSync, nd_for, ytm_client
from . import core
from .core import SECRET_MASK, SECRET_RETYPE, app, login_required, monitor, runner, same_target, store
from .matching import pl_id
from .services import audit, status_counts
from .store import AUTH_F, VERSION
from .sync import next_run_ts


# ------------------------------------------------------------------ API JSON
@app.route("/api/status")
@login_required
def api_status():
    lr = store.state.get("last_run") or {}
    return jsonify(running=runner.running, progress=runner.progress, last_run=lr, version=VERSION,
                   next_run=next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at),
                   monitor=dict(store.state.get("monitor", {}), busy=monitor.busy,
                                enabled=store.cfg["monitor"]["enabled"]),
                   mqtt=dict(enabled=store.cfg["mqtt"]["enabled"], connected=bool(core.mqtt_bridge and core.mqtt_bridge.connected),
                             error=core.mqtt_bridge.last_error if core.mqtt_bridge else None),
                   counts=status_counts())


@app.route("/api/logs")
@login_required
def api_logs():
    try:
        since = int(request.args.get("since", 0))
    except ValueError:
        since = 0
    return jsonify(lines=runner.logs_since(since))


@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    j = request.get_json(silent=True) or {}
    dry = j.get("dry")
    only = [j["playlist"]] if j.get("playlist") else None
    ok = runner.start(trigger="manuel", dry=None if dry is None else bool(dry), only=only)
    return jsonify(ok=ok, error=None if ok else "Une synchro est déjà en cours")


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    runner.cancel = True
    return jsonify(ok=True)


@app.route("/api/test/<what>", methods=["POST"])
@login_required
def api_test(what):
    f = request.get_json(silent=True) or {}
    try:
        if what == "navidrome":
            pw = f.get("password") or ""
            if pw in ("", SECRET_MASK):
                if not same_target(f.get("url"), store.cfg["navidrome"]["url"]) or \
                        (f.get("user") or "") != store.cfg["navidrome"]["user"]:
                    return jsonify(ok=False, msg=SECRET_RETYPE)
                pw = store.cfg["navidrome"]["password"]
            v = Navidrome(f.get("url"), f.get("user"), pw).ping()
            return jsonify(ok=True, msg=f"Connexion OK (Navidrome {v})")
        if what == "soulsync":
            key = (f.get("api_key") or "").strip()
            if key in ("", SECRET_MASK):
                if not same_target(f.get("url"), store.cfg["soulsync"]["url"]):
                    return jsonify(ok=False, msg=SECRET_RETYPE)
                key = store.cfg["soulsync"]["api_key"]
            d = SoulSync(f.get("url"), key, f.get("profile_id")).downloads()
            return jsonify(ok=True, msg=f"Connexion OK ({len(d)} téléchargement(s) en file)")
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))
    return jsonify(ok=False, msg="test inconnu")


@app.route("/api/youtube/library")
@login_required
def api_yt_library():
    try:
        have = {pl_id(p["source"]) for p in store.cfg["playlists"]}
        pls = ytm_client().get_library_playlists(limit=None)
        return jsonify(ok=True, playlists=[{"id": p["playlistId"], "title": p.get("title"),
                                            "count": p.get("count"), "added": p["playlistId"] in have}
                                           for p in pls if p.get("playlistId")])
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/soulsync/downloads")
@login_required
def api_ss_downloads():
    c = store.cfg["soulsync"]
    if not c["url"] or not c["api_key"]:
        return jsonify(ok=False, msg="SoulSync non configuré")
    try:
        return jsonify(ok=True, downloads=SoulSync(c["url"], c["api_key"], c["profile_id"]).downloads())
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


# ------------------------------------------------------------------ export Navidrome -> YouTube Music
@app.route("/api/navidrome/playlists")
@login_required
def api_nd_playlists():
    out = []
    try:
        for acc in [""] + [a["id"] for a in store.cfg.get("nd_accounts", [])]:
            nd = nd_for(store.cfg, acc)
            for p in nd.call("getPlaylists").get("playlists", {}).get("playlist", []):
                if p.get("owner") == nd.user:
                    out.append({"id": p["id"], "name": p["name"], "count": p.get("songCount"),
                                "account": acc, "owner": nd.user})
        return jsonify(ok=True, playlists=out, exports=store.state.get("exports", []))
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/export-ytm", methods=["POST"])
@login_required
def api_export_ytm():
    from .engine import EXPORT_LOCK, export_to_ytm
    j = request.get_json(silent=True) or {}
    if not os.path.exists(AUTH_F):
        return jsonify(ok=False, msg="Connecte d'abord ton compte YouTube (page Compte YouTube).")
    if EXPORT_LOCK.locked():
        return jsonify(ok=False, msg="Un export est déjà en cours.")
    name = (j.get("name") or "").strip()[:150]
    privacy = j.get("privacy") if j.get("privacy") in ("PRIVATE", "UNLISTED", "PUBLIC") else "PRIVATE"
    if not name or not j.get("id"):
        return jsonify(ok=False, msg="Choisis une playlist et un nom.")
    threading.Thread(target=export_to_ytm, args=(store, runner.log, j["id"], name, privacy, j.get("account", "")),
                     daemon=True).start()
    audit("export", [], f"Export de la playlist Navidrome vers YouTube Music « {name} »")
    return jsonify(ok=True)


# ------------------------------------------------------------------ comptes Navidrome supplementaires
@app.route("/api/accounts", methods=["POST"])
@login_required
def api_accounts():
    j = request.get_json(silent=True) or {}
    act = j.get("action")
    accs = store.cfg.setdefault("nd_accounts", [])
    if act == "add":
        user, pw = (j.get("user") or "").strip(), j.get("password") or ""
        if not user or not pw:
            return jsonify(ok=False, msg="Utilisateur et mot de passe requis")
        if user == store.cfg["navidrome"]["user"] or any(a["user"] == user for a in accs):
            return jsonify(ok=False, msg="Ce compte existe déjà")
        try:
            Navidrome(store.cfg["navidrome"]["url"], user, pw).ping()
        except Exception as ex:
            return jsonify(ok=False, msg=f"Connexion refusée : {ex}")
        accs.append({"id": uuid.uuid4().hex[:8], "user": user, "password": pw})
    elif act == "delete":
        store.cfg["nd_accounts"] = [a for a in accs if a["id"] != j.get("id")]
        for p in store.cfg["playlists"]:
            if p.get("nd_account") == j.get("id"):
                p["nd_account"] = ""
    store.save_cfg()
    return jsonify(ok=True)


# ------------------------------------------------------------------ notifications
@app.route("/api/test/notify/<channel>", methods=["POST"])
@login_required
def api_test_notify(channel):
    if channel not in notify.CHANNELS:
        return jsonify(ok=False, msg="canal inconnu")
    res = notify.send(store.cfg, "YTM Sync — test", f"Notification de test (v{VERSION}). Tout fonctionne !",
                      only=channel)
    err = res.get(channel)
    return jsonify(ok=not err, msg=err or "Envoyée — enregistre les paramètres si ce n'est pas déjà fait")


# ------------------------------------------------------------------ suivi SoulSync
@app.route("/api/monitor/check", methods=["POST"])
@login_required
def api_monitor_check():
    monitor.trigger()
    return jsonify(ok=True)


@app.route("/api/youtube/mixes")
@login_required
def api_yt_mixes():
    """Mixes et recommandations de l'accueil YouTube Music (Supermix, Decouvertes…)."""
    try:
        have = {pl_id(p["source"]) for p in store.cfg["playlists"]}
        out = []
        for sec in ytm_client().get_home(limit=8) or []:
            for it in sec.get("contents") or []:
                pid = (it or {}).get("playlistId")
                if pid and not it.get("videoId"):
                    out.append({"id": pl_id(pid), "title": it.get("title"), "section": sec.get("title"),
                                "description": it.get("description"), "added": pl_id(pid) in have})
        seen, uniq = set(), []
        for x in out:
            if x["id"] not in seen:
                seen.add(x["id"])
                uniq.append(x)
        return jsonify(ok=True, mixes=uniq)
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/test/mqtt", methods=["POST"])
@login_required
def api_test_mqtt():
    from .mqtt import MqttBridge
    f = request.get_json(silent=True) or {}
    c = dict(store.cfg["mqtt"])
    c.update({k: f[k] for k in ("host", "port", "user", "tls") if k in f})
    if f.get("password") not in (None, "", SECRET_MASK):
        c["password"] = f["password"]
    elif c.get("password") and (not same_target(f.get("host"), store.cfg["mqtt"]["host"])
                                 or (f.get("user") or "") != store.cfg["mqtt"]["user"]):
        return jsonify(ok=False, msg=SECRET_RETYPE)
    if not c.get("host"):
        return jsonify(ok=False, msg="Adresse du broker manquante")
    ok, msg = MqttBridge.test(c)
    return jsonify(ok=ok, msg=msg)
