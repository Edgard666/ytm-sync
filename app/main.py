"""YTM Sync — interface web."""
import datetime as dt
import functools
import os
import threading
import time
import uuid

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from waitress import serve
from werkzeug.security import check_password_hash, generate_password_hash

from .engine import (Navidrome, SoulSync, SyncRunner, next_run_ts, pl_id, scheduler_loop,
                     ytm_account, ytm_client, ytm_setup_from_headers)
from .store import AUTH_F, VERSION, Store

store = Store()
runner = SyncRunner(store)
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
    return {"version": VERSION}


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
        if check_password_hash(store.cfg["auth"]["password_hash"], request.form.get("password", "")):
            session.permanent, session["ok"] = True, True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") else "/")
        time.sleep(1)
        flash("Mot de passe incorrect.", "err")
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
                            "nd_name": request.form.get("nd_name", "").strip(), "enabled": True})
                flash("Playlist ajoutée.", "ok")
        else:
            p = next((x for x in pls if x["id"] == request.form.get("id")), None)
            if p and act == "save":
                p["nd_name"] = request.form.get("nd_name", "").strip()
                p["enabled"] = request.form.get("enabled") == "on"
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
                             strict_versions=f.get("strict_versions") == "on")
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
    lab = {"present": "Présent", "missing": "Manquant", "requested": "Demandé", "error": "Erreur"}

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
                "Erreur", "Lien YouTube"])
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
                   next_run=next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at))


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


@app.route("/api/tracks/rerequest", methods=["POST"])
@login_required
def api_rerequest():
    j = request.get_json(silent=True) or {}
    ids = j.get("ids")
    with store.lock:
        for vid, e in store.state["tracks"].items():
            if (ids is None or vid in ids) and e.get("status") != "present":
                e["requested_at"] = None
                if e.get("status") == "requested":
                    e["status"] = "missing"
        store.save_state()
    return jsonify(ok=True)


def main():
    threading.Thread(target=scheduler_loop, args=(store, runner), daemon=True).start()
    port = int(os.environ.get("WEBUI_PORT", "8095"))
    print(f"YTM Sync v{VERSION} — interface web sur le port {port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
