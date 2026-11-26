"""YTM Sync — interface web."""
import collections
import datetime as dt
import functools
import hmac
import json
import os
import secrets
import threading
import time
import uuid

from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash

from . import notify
from .engine import (DL_LABEL, Monitor, Navidrome, SoulSync, SyncRunner, nd_for, next_run_ts, pl_id,
                     scheduler_loop, soulsync_from, ytm_account, ytm_client, ytm_setup_from_headers)
from .store import AUTH_F, VERSION, Store

store = Store()
runner = SyncRunner(store)
monitor = Monitor(store, runner)
app = Flask(__name__)
app.secret_key = store.secret_key()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30))
SECRET_MASK = "••••••••"


@app.template_filter("dtf")
def dtf(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "—"


@app.context_processor
def inject():
    return {"version": VERSION, "csrf_token": csrf_token, "dl_label": DL_LABEL}


# ------------------------------------------------------------------ securite
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def client_ip():
    if store.cfg["security"].get("behind_proxy") and request.access_route:
        return request.access_route[0]
    return request.remote_addr or "?"


FAILS = collections.defaultdict(list)  # ip -> horodatages des echecs de connexion
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60


def _locked(ip):
    FAILS[ip] = [t for t in FAILS[ip] if time.time() - t < FAIL_WINDOW]
    return len(FAILS[ip]) >= MAX_FAILS


@app.before_request
def csrf_protect():
    if request.method == "POST" and not request.path.startswith("/api/ext/"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        if not session.get("csrf") or not hmac.compare_digest(sent, session["csrf"]):
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Jeton de sécurité invalide : recharge la page.", msg="Jeton de sécurité invalide : recharge la page."), 400
            flash("Session expirée : recommence.", "err")
            ref = request.referrer or "/"
            return redirect(ref if ref.startswith(request.host_url) else "/")


@app.after_request
def sec_headers(r):
    r.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return r


# ------------------------------------------------------------------ authentification
def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **kw):
        if not store.cfg["auth"]["password_hash"]:
            return redirect(url_for("setup"))
        if not session.get("ok"):
            if request.path.startswith("/api/"):
                return jsonify(error="non connecté"), 401
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrap


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if store.cfg["auth"]["password_hash"]:
        return redirect(url_for("login"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        if len(pw) < 6 or pw != pw2:
            flash("Mot de passe trop court (6 min.) ou différent de la confirmation.", "err")
        else:
            store.cfg["auth"]["password_hash"] = generate_password_hash(pw)
            store.save_cfg()
            session.permanent, session["ok"] = True, True
            return redirect(url_for("settings"))
    return render_template("login.html", setup=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not store.cfg["auth"]["password_hash"]:
        return redirect(url_for("setup"))
    if request.method == "POST":
        ip = client_ip()
        if _locked(ip):
            wait = int((FAIL_WINDOW - (time.time() - FAILS[ip][0])) / 60) + 1
            flash(f"Trop de tentatives : réessaie dans {wait} min.", "err")
        elif check_password_hash(store.cfg["auth"]["password_hash"], request.form.get("password", "")):
            FAILS.pop(ip, None)
            tok = session.get("csrf")
            session.clear()  # nouvelle session apres connexion
            session.permanent, session["ok"], session["csrf"] = True, True, tok or secrets.token_urlsafe(32)
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        else:
            FAILS[ip].append(time.time())
            time.sleep(1)
            left = MAX_FAILS - len(FAILS[ip])
            flash("Mot de passe incorrect." + (f" ({left} essai(s) restant(s))" if left <= 2 else ""), "err")
    return render_template("login.html", setup=False)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return "ok"


# ------------------------------------------------------------------ pages
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", page="dash", cfg=store.cfg, st=store.state)


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
            else:
                pls.append({"id": uuid.uuid4().hex[:8], "source": src,
                            "nd_name": request.form.get("nd_name", "").strip(), "enabled": True,
                            "nd_account": request.form.get("nd_account", "")})
                flash("Playlist ajoutée.", "ok")
        else:
            p = next((x for x in pls if x["id"] == request.form.get("id")), None)
            if p and act == "save":
                p["nd_name"] = request.form.get("nd_name", "").strip()
                p["enabled"] = request.form.get("enabled") == "on"
                p["nd_account"] = request.form.get("nd_account", "")
                flash("Playlist enregistrée.", "ok")
            elif p and act == "delete":
                pls.remove(p)
                store.state["playlists"].pop(p["id"], None)
                store.save_state()
                flash("Playlist retirée (la playlist Navidrome existante n'est pas supprimée).", "ok")
        store.save_cfg()
        return redirect(url_for("playlists"))
    return render_template("playlists.html", page="pl", cfg=store.cfg, st=store.state, pl_id=pl_id)


@app.route("/tracks")
@login_required
def tracks():
    names = {p["id"]: (store.state["playlists"].get(p["id"], {}).get("title") or pl_id(p["source"]))
             for p in store.cfg["playlists"]}
    rows = sorted(store.state.get("tracks", {}).values(),
                  key=lambda e: (e.get("status") == "present", (e.get("artist") or "").lower(), e.get("title") or ""))
    return render_template("tracks.html", page="tr", rows=rows, names=names)


@app.route("/youtube", methods=["GET", "POST"])
@login_required
def youtube():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "headers":
            try:
                ytm_setup_from_headers(request.form.get("headers", ""))
                flash(f"Connexion YouTube Music enregistrée ({ytm_account()}).", "ok")
            except Exception as ex:
                if os.path.exists(AUTH_F):
                    os.remove(AUTH_F)
                flash(f"En-têtes invalides : {ex}", "err")
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


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    c = store.cfg
    if request.method == "POST":
        f = request.form

        def keep(new, old):
            return old if new in ("", SECRET_MASK) else new

        c["navidrome"].update(url=f.get("nd_url", "").strip(), user=f.get("nd_user", "").strip(),
                              password=keep(f.get("nd_pass", ""), c["navidrome"]["password"]))
        c["soulsync"].update(url=f.get("ss_url", "").strip(), profile_id=f.get("ss_profile", "1").strip() or "1",
                             api_key=keep(f.get("ss_key", "").strip(), c["soulsync"]["api_key"]))
        try:
            c["sync"].update(dry_run=f.get("dry_run") == "on", trigger_process=f.get("trigger_process") == "on",
                             scan_before=f.get("scan_before") == "on",
                             title_threshold=min(1.0, max(0.3, float(f.get("title_threshold", 0.85)))),
                             artist_threshold=min(1.0, max(0.0, float(f.get("artist_threshold", 0.6)))),
                             re_request_days=max(0, int(f.get("re_request_days", 7))),
                             duration_tolerance=max(0, int(f.get("duration_tolerance", 15))),
                             strict_versions=f.get("strict_versions") == "on",
                             library_cache=f.get("library_cache") == "on",
                             exclude_rules=f.get("exclude_rules", "").strip())
            c["monitor"].update(enabled=f.get("mon_enabled") == "on", auto_scan=f.get("mon_scan") == "on",
                                resync_after=f.get("mon_resync") == "on",
                                interval_min=max(1, int(f.get("mon_interval", 5))))
            n = c["notify"]
            n.update(on_sync=f.get("nt_on_sync", "changes"), on_dl_fail=f.get("nt_dl_fail") == "on")
            n["ntfy"].update(enabled=f.get("ntfy_on") == "on", url=f.get("ntfy_url", "").strip() or "https://ntfy.sh",
                             topic=f.get("ntfy_topic", "").strip(),
                             token=keep(f.get("ntfy_token", "").strip(), n["ntfy"]["token"]))
            n["gotify"].update(enabled=f.get("gotify_on") == "on", url=f.get("gotify_url", "").strip(),
                               token=keep(f.get("gotify_token", "").strip(), n["gotify"]["token"]))
            n["email"].update(enabled=f.get("mail_on") == "on", host=f.get("mail_host", "").strip(),
                              port=int(f.get("mail_port") or 587), security=f.get("mail_sec", "starttls"),
                              user=f.get("mail_user", "").strip(), **{"from": f.get("mail_from", "").strip()},
                              to=f.get("mail_to", "").strip(),
                              password=keep(f.get("mail_pass", ""), n["email"]["password"]))
            c["security"]["behind_proxy"] = f.get("behind_proxy") == "on"
            c["nd_playlists"].update(enabled=f.get("ndpl_enabled") == "on", public=f.get("ndpl_public") == "on",
                                     prefix=f.get("ndpl_prefix", ""))
            c["schedule"].update(enabled=f.get("sch_enabled") == "on", mode=f.get("sch_mode", "interval"),
                                 interval_hours=max(0.25, float(f.get("sch_interval", 6))),
                                 daily_time=f.get("sch_daily", "03:00") or "03:00")
            c["logs_keep"] = max(1, int(f.get("logs_keep", 30)))
        except ValueError:
            flash("Valeur numérique invalide.", "err")
            return redirect(url_for("settings"))
        newpw = f.get("new_password", "")
        if newpw:
            if len(newpw) < 6:
                flash("Nouveau mot de passe trop court (6 min.) : non modifié.", "err")
            else:
                c["auth"]["password_hash"] = generate_password_hash(newpw)
        store.save_cfg()
        flash("Paramètres enregistrés.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", page="set", c=c, mask=SECRET_MASK)


@app.route("/api/settings/rules-preview", methods=["POST"])
@login_required
def api_rules_preview():
    """Titres qui seraient ignores par ces regles (d'apres la derniere synchro)."""
    from .engine import match_rule, parse_rules
    rules = parse_rules((request.get_json(silent=True) or {}).get("rules", ""))
    hits = []
    for e in store.state.get("tracks", {}).values():
        r = match_rule(rules, e.get("title"), e.get("artist"), e.get("album"))
        if r:
            hits.append(f"{e.get('artist')} - {e.get('title')}  ←  {r}")
    return jsonify(ok=True, count=len(hits), hits=sorted(hits)[:100])


@app.route("/export/tracks.csv")
@login_required
def export_tracks():
    """CSV (Excel FR : separateur ';', UTF-8 avec BOM) pour verifier les correspondances."""
    import csv
    import io
    from flask import Response
    want = request.args.get("status", "all")
    names = {p["id"]: (store.state["playlists"].get(p["id"], {}).get("title") or pl_id(p["source"]))
             for p in store.cfg["playlists"]}
    lab = {"present": "Présent", "missing": "Manquant", "requested": "Demandé", "error": "Erreur",
           "ignored": "Ignoré"}

    def mmss(sec):
        return f"{int(sec) // 60}:{int(sec) % 60:02d}" if sec else ""

    rows = [e for e in store.state.get("tracks", {}).values()
            if want == "all" or e.get("status") == want
            or (want == "missing" and e.get("status") in ("missing", "requested", "error"))]
    rows.sort(key=lambda e: (e.get("status") != "missing", (e.get("artist") or "").lower(), e.get("title") or ""))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Statut", "Artiste YouTube", "Titre YouTube", "Album YouTube", "Durée YouTube",
                "Artiste Navidrome", "Titre Navidrome", "Album Navidrome", "Durée Navidrome",
                "Fichier Navidrome", "Correction manuelle", "Avertissement", "Playlists", "Demandé à SoulSync le",
                "Téléchargement SoulSync", "Raison ignoré", "Erreur", "Lien YouTube"])
    for e in rows:
        w.writerow([lab.get(e.get("status"), e.get("status")), e.get("artist"), e.get("title"),
                    e.get("album") or "", mmss(e.get("duration")),
                    e.get("nd_artist") or "", e.get("nd_title") or "", e.get("nd_album") or "",
                    mmss(e.get("nd_duration")), e.get("nd_path") or "",
                    ("Lien manuel" if e.get("manual") else "") +
                    (f"{' + ' if e.get('manual') else ''}{e['blocked']} lien(s) refusé(s)" if e.get("blocked") else ""),
                    e.get("warn") or "",
                    ", ".join(names.get(p, "?") for p in e.get("playlists", [])),
                    dtf(e.get("requested_at")) if e.get("requested_at") else "",
                    (DL_LABEL.get(e.get("dl_status"), "") + (f" — {e['dl_error']}" if e.get("dl_error") else ""))
                    if e.get("status") == "requested" else "",
                    e.get("ignore_reason") or "" if e.get("status") == "ignored" else "",
                    e.get("error") or "", f"https://music.youtube.com/watch?v={e.get('videoId')}"])
    fname = f"ytm-sync_{want}_{dt.datetime.now():%Y%m%d_%H%M}.csv"
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# ------------------------------------------------------------------ API JSON
@app.route("/api/status")
@login_required
def api_status():
    lr = store.state.get("last_run") or {}
    return jsonify(running=runner.running, progress=runner.progress, last_run=lr, version=VERSION,
                   next_run=next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at),
                   monitor=dict(store.state.get("monitor", {}), busy=monitor.busy,
                                enabled=store.cfg["monitor"]["enabled"]),
                   counts=status_counts())


@app.route("/api/logs")
@login_required
def api_logs():
    return jsonify(lines=runner.logs_since(int(request.args.get("since", 0))))


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
                pw = store.cfg["navidrome"]["password"]
            v = Navidrome(f.get("url"), f.get("user"), pw).ping()
            return jsonify(ok=True, msg=f"Connexion OK (Navidrome {v})")
        if what == "soulsync":
            key = (f.get("api_key") or "").strip()
            if key in ("", SECRET_MASK):
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


def _nd():
    c = store.cfg["navidrome"]
    return Navidrome(c["url"], c["user"], c["password"])


def _nd_fields(song):
    return dict(nd_id=song["id"], nd_artist=song.get("artist"), nd_title=song.get("title"),
                nd_album=song.get("album"), nd_duration=song.get("duration"), nd_path=song.get("path"))


@app.route("/api/navidrome/search")
@login_required
def api_nd_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(ok=True, songs=[])
    try:
        songs = _nd().search_songs(q, 40)
        return jsonify(ok=True, songs=[{"id": x["id"], "artist": x.get("artist"), "title": x.get("title"),
                                        "album": x.get("album"), "duration": x.get("duration"),
                                        "path": x.get("path")} for x in songs])
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/tracks/<vid>/link", methods=["POST"])
@login_required
def api_link(vid):
    nd_id = (request.get_json(silent=True) or {}).get("nd_id")
    try:
        song = _nd().get_song(nd_id)
    except Exception as ex:
        return jsonify(ok=False, msg=f"Morceau Navidrome introuvable : {ex}")
    with store.lock:
        ov = store.cfg.setdefault("overrides", {}).setdefault(vid, {})
        ov["nd_id"] = nd_id
        ov["blocked"] = [b for b in ov.get("blocked", []) if b != nd_id]
        store.save_cfg()
        e = store.state["tracks"].get(vid)
        if e:
            e.update(status="present", manual=True, requested_at=None, error=None, warn=None, **_nd_fields(song))
            store.save_state()
    return jsonify(ok=True)


@app.route("/api/tracks/<vid>/unlink", methods=["POST"])
@login_required
def api_unlink(vid):
    with store.lock:
        e = store.state["tracks"].get(vid) or {}
        ov = store.cfg.setdefault("overrides", {}).setdefault(vid, {})
        if e.get("nd_id") and e["nd_id"] not in ov.setdefault("blocked", []):
            ov["blocked"].append(e["nd_id"])
        ov.pop("nd_id", None)
        store.save_cfg()
        if e:
            e.update(status="missing", manual=False, blocked=len(ov["blocked"]), nd_id=None, nd_artist=None,
                     nd_title=None, nd_album=None, nd_duration=None, nd_path=None, warn=None)
            store.save_state()
    return jsonify(ok=True)


@app.route("/api/tracks/<vid>/reset", methods=["POST"])
@login_required
def api_reset(vid):
    with store.lock:
        store.cfg.setdefault("overrides", {}).pop(vid, None)
        store.save_cfg()
        e = store.state["tracks"].get(vid)
        if e:
            e.update(manual=False, blocked=0)
            store.save_state()
    return jsonify(ok=True)


@app.route("/api/tracks/<vid>/ignore", methods=["POST"])
@login_required
def api_ignore(vid):
    on = (request.get_json(silent=True) or {}).get("ignore", True)
    with store.lock:
        ign = store.cfg.setdefault("ignored", {})
        e = store.state["tracks"].get(vid)
        to_remove = []
        if on:
            key = (e or {}).get("key")
            ign[vid] = {"artist": (e or {}).get("artist"), "title": (e or {}).get("title"), "key": key,
                        "at": time.time()}
            for x in store.state["tracks"].values():  # meme morceau sous une autre video : ignore aussi
                if x["videoId"] == vid or (key and x.get("key") == key):
                    if x.get("status") == "requested":
                        to_remove.append(x["videoId"])
                    x.update(status="ignored", nd_id=None, warn=None, error=None, requested_at=None,
                             dl_status=None, dl_progress=None, dl_error=None,
                             ignore_reason="ignoré manuellement" if x["videoId"] == vid
                             else "même morceau qu'un titre ignoré")
        else:
            ign.pop(vid, None)
            key = (e or {}).get("key")
            for x in store.state["tracks"].values():
                if x["videoId"] == vid or (key and x.get("key") == key and x.get("status") == "ignored"):
                    x.update(status="missing", ignore_reason=None)  # reevalue a la prochaine synchro
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
    return jsonify(ok=True, removed=removed)


@app.route("/api/tracks/rerequest", methods=["POST"])
@login_required
def api_rerequest():
    j = request.get_json(silent=True) or {}
    ids = j.get("ids")
    with store.lock:
        for vid, e in store.state["tracks"].items():
            if (ids is None or vid in ids) and e.get("status") not in ("present", "ignored"):
                e["requested_at"] = None
                if e.get("status") == "requested":
                    e["status"] = "missing"
        store.save_state()
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


@app.route("/cleanup")
@login_required
def cleanup():
    return render_template("cleanup.html", page="cl", rows=orphans(),
                           total=len(store.state.get("downloaded", {})))


@app.route("/export/cleanup.csv")
@login_required
def export_cleanup():
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Artiste", "Titre", "Album Navidrome", "Fichier Navidrome", "Arrivé le", "Raison", "Lien YouTube"])
    for o in orphans():
        w.writerow([o.get("nd_artist") or o.get("artist"), o.get("nd_title") or o.get("title"),
                    o.get("nd_album") or "", o.get("nd_path") or "", dtf(o.get("at")), o["reason"],
                    f"https://music.youtube.com/watch?v={o['videoId']}"])
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=ytm-sync_menage_{dt.datetime.now():%Y%m%d_%H%M}.csv"})


@app.route("/api/cleanup/forget", methods=["POST"])
@login_required
def api_cleanup_forget():
    """Retire des titres de la liste de menage (le fichier n'est jamais touche)."""
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    with store.lock:
        for vid in ids:
            store.state.get("downloaded", {}).pop(vid, None)
        store.save_state()
    return jsonify(ok=True)


# ------------------------------------------------------------------ sauvegarde / restauration
SECRET_PATHS = [("navidrome", "password"), ("soulsync", "api_key"), ("security", "api_key")]


@app.route("/backup.json")
@login_required
def backup():
    import copy
    cfg = copy.deepcopy(store.cfg)
    cfg.pop("auth", None)
    with_secrets = request.args.get("secrets") == "1"
    if not with_secrets:
        for a, b in SECRET_PATHS:
            cfg[a][b] = ""
        for a in cfg.get("nd_accounts", []):
            a["password"] = ""
        cfg["notify"]["ntfy"]["token"] = cfg["notify"]["gotify"]["token"] = cfg["notify"]["email"]["password"] = ""
    data = {"app": "ytm-sync", "version": VERSION, "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
            "with_secrets": with_secrets, "config": cfg,
            "downloaded": store.state.get("downloaded", {})}
    return Response(json.dumps(data, indent=1, ensure_ascii=False), content_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=ytm-sync_sauvegarde_{dt.datetime.now():%Y%m%d_%H%M}.json"})


@app.route("/restore", methods=["POST"])
@login_required
def restore():
    f = request.files.get("file")
    try:
        data = json.load(f)
        assert data.get("app") == "ytm-sync" and isinstance(data.get("config"), dict)
    except Exception:
        flash("Fichier de sauvegarde invalide.", "err")
        return redirect(url_for("settings") + "#backup")
    new = data["config"]
    new.pop("auth", None)
    with store.lock:
        old = store.cfg
        # secrets vides dans la sauvegarde : on garde ceux actuellement configures
        for a, b in SECRET_PATHS:
            if not (new.get(a) or {}).get(b):
                new.setdefault(a, {})[b] = old.get(a, {}).get(b, "")
        for ch, key in (("ntfy", "token"), ("gotify", "token"), ("email", "password")):
            if not new.get("notify", {}).get(ch, {}).get(key):
                new.setdefault("notify", {}).setdefault(ch, {})[key] = old["notify"][ch][key]
        olda = {a["user"]: a["password"] for a in old.get("nd_accounts", [])}
        for a in new.get("nd_accounts", []):
            a["password"] = a.get("password") or olda.get(a["user"], "")
        from .store import DEFAULT_CFG, _merge
        merged = _merge(new, DEFAULT_CFG)
        merged["auth"] = old["auth"]
        store.cfg.clear()
        store.cfg.update(merged)
        store.save_cfg()
        if request.form.get("with_history") == "on" and isinstance(data.get("downloaded"), dict):
            store.state.setdefault("downloaded", {}).update(data["downloaded"])
            store.save_state()
    flash(f"Sauvegarde du {data.get('exported_at', '?')} restaurée ({len(merged['playlists'])} playlists, "
          f"{len(merged['ignored'])} ignorés, {len(merged['overrides'])} corrections).", "ok")
    return redirect(url_for("settings"))


# ------------------------------------------------------------------ API externe (Home Assistant)
@app.route("/api/apikey", methods=["POST"])
@login_required
def api_apikey():
    act = (request.get_json(silent=True) or {}).get("action")
    store.cfg["security"]["api_key"] = secrets.token_urlsafe(32) if act == "generate" else ""
    store.save_cfg()
    return jsonify(ok=True, key=store.cfg["security"]["api_key"])


def ext_auth(f):
    @functools.wraps(f)
    def wrap(*a, **kw):
        key = store.cfg["security"].get("api_key")
        sent = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not key or not sent or not hmac.compare_digest(sent, key):
            return jsonify(ok=False, error="clé API invalide"), 401
        return f(*a, **kw)
    return wrap


def status_counts():
    c = collections.Counter(e.get("status") for e in store.state.get("tracks", {}).values())
    d = collections.Counter(e.get("dl_status") for e in store.state.get("tracks", {}).values()
                            if e.get("status") == "requested")
    return {"present": c["present"], "missing": c["missing"], "requested": c["requested"],
            "ignored": c["ignored"], "error": c["error"], "dl_failed": d["failed"], "dl_queued": d["queued"], "dl_done": d["completed"] + d["gone"],
            "dl_active": d["pending"] + d["searching"] + d["downloading"], "orphans": len(orphans())}


@app.route("/api/ext/status")
@ext_auth
def ext_status():
    lr = store.state.get("last_run") or {}
    nxt = next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at)
    return jsonify(ok=True, version=VERSION, running=runner.running, progress=runner.progress,
                   state="en cours" if runner.running else "au repos",
                   last_run={k: lr.get(k) for k in ("start", "end", "status", "trigger", "total", "present",
                                                    "missing", "sent", "errors", "dry")},
                   last_run_iso=dt.datetime.fromtimestamp(lr["start"]).astimezone().isoformat() if lr.get("start") else None,
                   next_run_iso=dt.datetime.fromtimestamp(nxt).astimezone().isoformat() if nxt else None,
                   counts=status_counts())


@app.route("/api/ext/sync", methods=["POST"])
@ext_auth
def ext_sync():
    j = request.get_json(silent=True) or {}
    dry = j.get("dry", request.args.get("dry"))
    dry = None if dry is None else str(dry).lower() in ("1", "true", "yes", "oui")
    only = [j["playlist"]] if j.get("playlist") else None
    ok = runner.start(trigger="Home Assistant", dry=dry, only=only)
    return jsonify(ok=ok, error=None if ok else "Une synchro est déjà en cours"), (200 if ok else 409)


def main():
    threading.Thread(target=scheduler_loop, args=(store, runner), daemon=True).start()
    threading.Thread(target=monitor.loop, daemon=True).start()
    port = int(os.environ.get("WEBUI_PORT", "8095"))
    print(f"YTM Sync v{VERSION} — interface web sur le port {port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
