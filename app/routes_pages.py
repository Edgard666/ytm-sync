"""Routes : pages de l'interface (tableau de bord, playlists, titres, journal, compte YouTube, menage, diagnostic, PWA)."""
import os
import uuid

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from .clients import nd_for, soulsync_from, ytm_account, ytm_setup_from_headers
from . import core
from .core import LAST_PEER, LOG_RE, app, dtf, login_required, runner, store
from .matching import pl_id
from .services import _name_taken, _nd, audit, list_backups, orphans, setup_steps
from .store import AUTH_F, VERSION
from .sync import next_run_ts


@app.route("/")
@login_required
def dashboard():
    steps = setup_steps()
    show = not store.cfg.get("ui", {}).get("hide_wizard") and not all(x[1] for x in steps)
    return render_template("dashboard.html", page="dash", cfg=store.cfg, st=store.state,
                           steps=steps if show else None, ytm_auth_error=store.state.get("ytm_auth_error"),
                           recovered=store.recovered)


@app.route("/api/coverage")
@login_required
def api_coverage():
    return jsonify(ok=True, points=store.state.get("coverage", []))


@app.route("/api/wizard/hide", methods=["POST"])
@login_required
def api_wizard_hide():
    store.cfg.setdefault("ui", {})["hide_wizard"] = True
    store.save_cfg()
    return jsonify(ok=True)


@app.route("/logs")
@login_required
def logs_list():
    from .store import LOG_DIR
    files = sorted((f for f in os.listdir(LOG_DIR) if LOG_RE.match(f)), reverse=True)
    runs = {h.get("log"): h for h in store.state.get("history", []) if h.get("log")}
    return render_template("logs.html", page="logs", files=files, runs=runs, content=None, name=None)


@app.route("/logs/<name>")
@login_required
def log_view(name):
    from .store import LOG_DIR
    if not LOG_RE.match(name) or not os.path.exists(os.path.join(LOG_DIR, name)):
        abort(404)
    if request.args.get("raw"):
        return Response(open(os.path.join(LOG_DIR, name), encoding="utf-8").read(),
                        content_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={name}"})
    content = open(os.path.join(LOG_DIR, name), encoding="utf-8").read()
    files = sorted((f for f in os.listdir(LOG_DIR) if LOG_RE.match(f)), reverse=True)
    runs = {h.get("log"): h for h in store.state.get("history", []) if h.get("log")}
    return render_template("logs.html", page="logs", files=files, runs=runs, content=content, name=name)


@app.route("/playlists", methods=["GET", "POST"])
@login_required
def playlists():
    if request.method == "POST":
        act = request.form.get("action")
        pls = store.cfg["playlists"]
        if act == "add":
            src = request.form.get("source", "").strip()
            if not pl_id(src):
                flash("URL ou ID de playlist invalide.", "err")
            elif any(pl_id(p["source"]) == pl_id(src) for p in pls):
                flash("Cette playlist est déjà surveillée.", "err")
            elif _name_taken(request.form.get("nd_name", ""), request.form.get("nd_account", "")):
                flash("Ce nom de playlist Navidrome est déjà utilisé par une autre playlist.", "err")
            else:
                pls.append({"id": uuid.uuid4().hex[:8], "source": src,
                            "nd_name": request.form.get("nd_name", "").strip(), "enabled": True,
                            "nd_account": request.form.get("nd_account", "")})
                flash("Playlist ajoutée.", "ok")
        else:
            p = next((x for x in pls if x["id"] == request.form.get("id")), None)
            if p and act == "save" and _name_taken(request.form.get("nd_name", ""),
                                                    request.form.get("nd_account", ""), p["id"]):
                flash("Ce nom de playlist Navidrome est déjà utilisé par une autre playlist.", "err")
            elif p and act == "save":
                p["nd_name"] = request.form.get("nd_name", "").strip()
                p["enabled"] = request.form.get("enabled") == "on"
                p["nd_account"] = request.form.get("nd_account", "")
                flash("Playlist enregistrée.", "ok")
            elif p and act in ("delete", "delete_nd"):
                st_pl = store.state["playlists"].get(p["id"], {})
                msg = "Playlist retirée (la playlist Navidrome existante n'est pas supprimée)."
                if act == "delete_nd" and st_pl.get("nd_pl_id"):
                    try:
                        nd_for(store.cfg, st_pl.get("nd_account", "")).call("deletePlaylist", id=st_pl["nd_pl_id"])
                        msg = f"Playlist retirée et « {st_pl.get('nd_name')} » supprimée de Navidrome."
                    except Exception as ex:
                        msg = f"Playlist retirée, mais suppression dans Navidrome impossible : {ex}"
                pls.remove(p)
                if p.get("auto"):  # ajoutee par la surveillance automatique : ne pas la rajouter
                    store.cfg["auto_watch"].setdefault("dismissed", []).append(pl_id(p["source"]))
                store.state["playlists"].pop(p["id"], None)
                store.save_state()
                audit("playlist", [], f"Playlist « {st_pl.get('title') or p['source']} » retirée"
                      + (" (+ supprimée de Navidrome)" if act == "delete_nd" else ""))
                flash(msg, "ok")
        store.save_cfg()
        return redirect(url_for("playlists"))
    return render_template("playlists.html", page="pl", cfg=store.cfg, st=store.state, pl_id=pl_id)


@app.route("/tracks")
@login_required
def tracks():
    return render_template("tracks.html", page="tr")


@app.route("/youtube", methods=["GET", "POST"])
@login_required
def youtube():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "headers":
            try:
                name = ytm_setup_from_headers(request.form.get("headers", ""))
                store.state["ytm_auth_error"] = None
                flash(f"Connexion YouTube Music enregistrée ({name}).", "ok")
            except Exception as ex:
                flash(f"En-têtes invalides : {ex}"
                      + (" — la session précédente est conservée." if os.path.exists(AUTH_F) else ""), "err")
        elif act == "logout" and os.path.exists(AUTH_F):
            os.remove(AUTH_F)
            flash("Session YouTube Music supprimée.", "ok")
        elif act == "import":
            have = {pl_id(p["source"]) for p in store.cfg["playlists"]}
            n = 0
            for pid in request.form.getlist("pl"):
                if pid not in have:
                    store.cfg["playlists"].append({"id": uuid.uuid4().hex[:8], "source": pid,
                                                   "nd_name": "", "enabled": True})
                    n += 1
            store.save_cfg()
            flash(f"{n} playlist(s) ajoutée(s).", "ok")
            return redirect(url_for("playlists"))
        return redirect(url_for("youtube"))
    account, err = None, None
    try:
        account = ytm_account()
    except Exception as ex:
        err = str(ex)
    return render_template("youtube.html", page="yt", account=account, err=err,
                           connected=os.path.exists(AUTH_F))


# ------------------------------------------------------------------ diagnostic
@app.route("/diagnostic")
@login_required
def diagnostic():
    return render_template("diagnostic.html", page="diag")


@app.route("/api/diagnostic")
@login_required
def api_diagnostic():
    """Verifie tout d'un coup. Chaque ligne : (section, element, ok|warn|err|info, detail)."""
    import platform
    import shutil
    from .store import CFG_F, CONFIG_DIR
    out = []

    def add(sec, item, st, detail=""):
        out.append({"section": sec, "item": item, "status": st, "detail": str(detail)})
    c = store.cfg
    # --- application
    try:
        import ytmusicapi
        ytv = getattr(ytmusicapi, "__version__", "?")
    except Exception:
        ytv = "?"
    add("Application", "Version", "info", f"YTM Sync {VERSION} · Python {platform.python_version()} · ytmusicapi {ytv}")
    uid = os.getuid() if hasattr(os, "getuid") else "?"
    add("Application", "Utilisateur du conteneur", "ok" if uid != 0 else "warn",
        f"uid {uid}" + (" (root : redéploie avec PUID/PGID)" if uid == 0 else ""))
    try:
        mode = oct(os.stat(CFG_F).st_mode & 0o777)
        add("Application", "Protection de config.json", "ok" if mode == "0o600" else "warn", mode)
    except OSError:
        pass
    for msg in store.recovered:
        add("Application", "Fichier récupéré au démarrage", "warn", msg)
    bks = list_backups()
    add("Application", "Sauvegardes quotidiennes", "ok" if bks else "info",
        f"{len(bks)} jour(s), la plus récente le {bks[0]['day'][6:]}/{bks[0]['day'][4:6]}" if bks else "aucune pour l'instant")
    du = shutil.disk_usage(CONFIG_DIR)
    add("Application", "Espace libre (appdata)", "ok" if du.free > 500e6 else "warn", f"{du.free / 1e9:.1f} Go")
    # --- Navidrome
    try:
        nd = _nd()
        add("Navidrome", "Connexion", "ok", f"Navidrome {nd.ping()} ({c['navidrome']['url']})")
        try:
            u = nd.call("getUser", username=c["navidrome"]["user"]).get("user", {})
            add("Navidrome", "Droits administrateur", "ok" if u.get("adminRole") else "warn",
                "oui" if u.get("adminRole") else "non : le scan automatique après téléchargement ne fonctionnera pas")
        except Exception as ex:
            add("Navidrome", "Droits administrateur", "info", f"non vérifiable ({ex})")
        st = nd.call("getScanStatus").get("scanStatus", {})
        add("Navidrome", "Bibliothèque", "ok", f"{st.get('count', '?')} morceaux · dernier scan {st.get('lastScan', '?')}")
    except Exception as ex:
        add("Navidrome", "Connexion", "err", ex)
    for a in c.get("nd_accounts", []):
        try:
            nd_for(c, a["id"]).ping()
            add("Navidrome", f"Compte {a['user']}", "ok", "connexion OK")
        except Exception as ex:
            add("Navidrome", f"Compte {a['user']}", "err", ex)
    # --- SoulSync
    ss = soulsync_from(c)
    if not ss:
        add("SoulSync", "Configuration", "warn", "non configuré : les manquants sont seulement listés")
    else:
        try:
            add("SoulSync", "Connexion", "ok", f"{len(ss.downloads())} téléchargement(s) · {len(ss.wishlist())} titre(s) en wishlist")
        except Exception as ex:
            add("SoulSync", "Connexion", "err", ex)
    m = store.state.get("monitor", {})
    if c["monitor"]["enabled"]:
        add("SoulSync", "Suivi des téléchargements", "err" if m.get("last_error") else "ok",
            m.get("last_error") or (f"dernière vérification {dtf(m.get('last_check'))}" if m.get("last_check") else "pas encore lancé"))
    # --- YouTube
    if os.path.exists(AUTH_F):
        try:
            add("YouTube Music", "Compte connecté", "ok", ytm_account())
        except Exception as ex:
            add("YouTube Music", "Compte connecté", "err", f"session expirée ou invalide : {ex}")
    else:
        add("YouTube Music", "Compte connecté", "info", "non (seules les playlists publiques / non répertoriées sont lisibles)")
    bad = [x for x in store.state.get("playlists", {}).values() if x.get("error")]
    add("YouTube Music", "Playlists", "warn" if bad else "ok",
        f"{len(c['playlists'])} surveillée(s)" + (f", {len(bad)} en erreur : " + "; ".join((x.get('title') or '?') for x in bad) if bad else ""))
    # --- synchro
    lr = store.state.get("last_run") or {}
    add("Synchronisation", "Dernière synchro", "err" if lr.get("status") == "erreur" else ("ok" if lr else "info"),
        f"{dtf(lr.get('start'))} — {lr.get('status')} {lr.get('message') or ''}" if lr else "aucune")
    nxt = next_run_ts(c["schedule"], lr.get("start"), runner.started_at)
    add("Synchronisation", "Planification", "ok" if nxt else "info", dtf(nxt) if nxt else "désactivée")
    add("Synchronisation", "Mode", "warn" if c["sync"]["dry_run"] else "ok",
        "simulation par défaut (rien n'est modifié)" if c["sync"]["dry_run"] else "réel")
    # --- notifications / MQTT / securite
    chans = [k for k in ("ntfy", "gotify", "email", "webhook") if c["notify"].get(k, {}).get("enabled")]
    add("Notifications", "Canaux actifs", "ok" if chans else "info", ", ".join(chans) or "aucun")
    if c["notify"]["ntfy"].get("enabled") and "ntfy.sh" in c["notify"]["ntfy"].get("url", "") and len(c["notify"]["ntfy"].get("topic", "")) < 16:
        add("Notifications", "Sujet ntfy", "warn", "nom court sur le serveur public ntfy.sh : facile à deviner")
    if c["mqtt"]["enabled"]:
        add("Home Assistant", "MQTT", "ok" if core.mqtt_bridge and core.mqtt_bridge.connected else "err",
            "connecté" if core.mqtt_bridge and core.mqtt_bridge.connected else (core.mqtt_bridge.last_error if core.mqtt_bridge else "non démarré"))
    add("Home Assistant", "API REST", "info", "activée" if c["security"].get("api_key_hash") else "désactivée")
    sec = c["security"]
    if sec.get("behind_proxy"):
        add("Sécurité", "Reverse proxy", "ok" if sec.get("proxy_ips") else "warn",
            f"adresse(s) de confiance : {sec['proxy_ips']}" if sec.get("proxy_ips") else
            f"aucune adresse de proxy renseignée : seuls les appareils du réseau local sont crus. Renseigne l'IP "
            f"du proxy pour plus de sécurité (dernière adresse vue : {LAST_PEER['addr']})")
    add("Sécurité", "Connexion à l'interface", "ok" if request.is_secure else "info",
        "HTTPS" if request.is_secure else "HTTP (normal en local ; passe par Zoraxy en HTTPS pour un accès extérieur)")
    return jsonify(ok=True, checks=out)


# ------------------------------------------------------------------ appli installable (PWA)
@app.route("/manifest.webmanifest")
def manifest():
    return jsonify(name="YTM Sync", short_name="YTM Sync", start_url="/", display="standalone",
                   background_color="#0f1218", theme_color="#171c25", lang="fr",
                   icons=[{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                          {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"}])


@app.route("/sw.js")
def service_worker():
    # aucune mise en cache : les donnees restent privees et toujours a jour
    js = "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>self.clients.claim());" \
         "self.addEventListener('fetch',e=>{});"
    return Response(js, content_type="application/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/cleanup")
@login_required
def cleanup():
    return render_template("cleanup.html", page="cl", rows=orphans(),
                           total=len(store.state.get("downloaded", {})))
