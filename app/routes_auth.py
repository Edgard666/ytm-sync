"""Routes : installation, connexion, deconnexion, sessions."""
import hmac
import os
import secrets
import time

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from . import core
from .core import (
    FAILS,
    FAIL_WINDOW,
    MAX_FAILS,
    MIN_PW,
    _locked,
    app,
    client_ip,
    login_required,
    new_epoch,
    safe_next,
    store)
from .services import audit


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if store.cfg["auth"]["password_hash"]:
        return redirect(url_for("login"))
    if request.method == "POST":
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        code = request.form.get("code", "").strip().upper().replace(" ", "")
        setup_code = core.SETUP_CODE   # lu dans le noyau : defini au demarrage
        ip = client_ip()
        if _locked(ip):
            flash("Trop de tentatives : réessaie dans 15 min.", "err")
        elif not setup_code or not hmac.compare_digest(code, setup_code.replace("-", "")) and \
                not hmac.compare_digest(code, setup_code):
            FAILS[ip].append(time.time())   # meme blocage qu'a la connexion (sans pause qui bloquerait le serveur)
            flash("Code d'installation incorrect : il est affiché dans les journaux du conteneur "
                  "(Unraid → Docker → icône ytm-sync → Logs).", "err")
        elif len(pw) < MIN_PW or pw != pw2:
            flash(f"Mot de passe trop court ({MIN_PW} caractères min.) ou différent de la confirmation.", "err")
        else:
            store.cfg["auth"]["password_hash"] = generate_password_hash(pw)
            new_epoch()
            store.save_cfg()
            session.permanent, session["ok"], session["ep"] = True, True, store.cfg["auth"]["epoch"]
            session["seen"] = time.time()
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
            session["ep"] = store.cfg["auth"]["epoch"]
            session["seen"] = time.time()
            return redirect(safe_next(request.args.get("next", "/")))
        else:
            # pas de pause : elle bloquerait un des fils du serveur ; le blocage apres 5 essais suffit
            FAILS[ip].append(time.time())
            left = MAX_FAILS - len(FAILS[ip])
            flash("Mot de passe incorrect." + (f" ({left} essai(s) restant(s))" if left <= 2 else ""), "err")
    return render_template("login.html", setup=False)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/sessions/revoke", methods=["POST"])
@login_required
def api_revoke_sessions():
    """Deconnecte tous les appareils (sauf celui-ci)."""
    new_epoch()
    session["ep"] = store.cfg["auth"]["epoch"]
    store.save_cfg()
    audit("sessions", [], "Toutes les autres sessions ont été déconnectées")
    return jsonify(ok=True)


@app.route("/healthz")
def healthz():
    """Sante pour Docker : stockage accessible en ecriture et planificateur vivant (aucun appel externe)."""
    from .store import CONFIG_DIR
    from .sync import HEARTBEAT
    problems = []
    if not os.access(CONFIG_DIR, os.W_OK):
        problems.append("dossier /config non accessible en écriture")
    hb = HEARTBEAT.get("scheduler")
    if hb and time.time() - hb > 300:
        problems.append("planificateur bloqué")
    if problems:
        return jsonify(ok=False, problems=problems), 503
    return jsonify(ok=True)
