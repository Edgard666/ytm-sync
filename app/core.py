"""Noyau de l'interface web : application Flask, etat partage, securite (CSRF, CSP, en-tetes, compression), connexion."""
import collections
import datetime as dt
import functools
import hmac
import re
import secrets
import time

from flask import Flask, flash, jsonify, redirect, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from .monitor import DL_LABEL, Monitor
from .store import Store, VERSION
from .sync import SyncRunner


store = Store()
runner = SyncRunner(store)
monitor = Monitor(store, runner)
mqtt_bridge = None
app = Flask(__name__)
app.secret_key = store.secret_key()


class _ProxyAware:
    """Derriere Zoraxy (option activee) : IP et schema (https) reels du visiteur."""

    def __init__(self, wsgi):
        from werkzeug.middleware.proxy_fix import ProxyFix
        self.wsgi, self.fixed = wsgi, ProxyFix(wsgi, x_for=1, x_proto=1, x_host=1)

    def __call__(self, environ, start_response):
        sec = store.cfg["security"]
        peer = environ.get("REMOTE_ADDR", "")
        LAST_PEER["addr"] = peer
        trusted = [x.strip() for x in (sec.get("proxy_ips") or "").replace(";", ",").split(",") if x.strip()]
        # les en-tetes X-Forwarded-* ne sont crus que s'ils viennent du proxy (sinon : falsifiables)
        if sec.get("behind_proxy") and (not trusted or peer in trusted):
            return self.fixed(environ, start_response)
        return self.wsgi(environ, start_response)


LAST_PEER = {"addr": ""}  # derniere adresse directe vue (aide a renseigner l'IP du proxy)


app.wsgi_app = _ProxyAware(app.wsgi_app)


class _SessionInterface(SecureCookieSessionInterface):
    def get_cookie_secure(self, app_):
        return request.is_secure  # cookie « Secure » automatiquement quand l'acces est en HTTPS


app.session_interface = _SessionInterface()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=30),
                  MAX_CONTENT_LENGTH=20 * 1024 * 1024)  # 20 Mo max par envoi (restauration…)


@app.errorhandler(413)
def too_large(_e):
    if request.path.startswith("/api/"):
        return jsonify(ok=False, msg="Fichier trop volumineux (20 Mo maximum)"), 413
    flash("Fichier trop volumineux (20 Mo maximum).", "err")
    return redirect(url_for("settings"))
SECRET_MASK = "••••••••"
CLEAR_MARK = "__EFFACER__"
MIN_PW = 10
SETUP_CODE = None  # code d'installation (affiche dans les journaux du conteneur) tant qu'aucun mot de passe


@app.template_filter("dtf")
def dtf(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "—"


@app.context_processor
def inject():
    return {"version": VERSION, "csrf_token": csrf_token, "dl_label": DL_LABEL, "csp_nonce": csp_nonce}


def csp_nonce():
    """Jeton unique par page : seuls les scripts de l'application qui le portent peuvent s'executer."""
    from flask import g
    if "nonce" not in g:
        g.nonce = secrets.token_urlsafe(16)
    return g.nonce


# ------------------------------------------------------------------ securite
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def client_ip():
    return request.remote_addr or "?"  # deja corrigee par _ProxyAware si l'option reverse proxy est active


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
    if r.mimetype == "text/html":
        r.headers.setdefault("Content-Security-Policy",
                             f"default-src 'self'; script-src 'self' 'nonce-{csp_nonce()}'; "
                             "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                             "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'; "
                             "manifest-src 'self'; worker-src 'self'")
    r.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    return _gzip(r)


COMPRESSIBLE = {"application/json", "text/html", "text/csv", "text/plain", "text/css", "application/javascript"}


def _gzip(r):
    """Compression gzip des reponses texte (liste des titres : ~5 a 10 fois plus legere)."""
    import gzip
    if (r.direct_passthrough or r.status_code != 200 or r.mimetype not in COMPRESSIBLE
            or "gzip" not in request.headers.get("Accept-Encoding", "") or "Content-Encoding" in r.headers):
        return r
    data = r.get_data()
    if len(data) < 1500:
        return r
    r.set_data(gzip.compress(data, compresslevel=5))
    r.headers["Content-Encoding"] = "gzip"
    r.headers["Content-Length"] = str(len(r.get_data()))
    r.headers.add("Vary", "Accept-Encoding")
    return r


# ------------------------------------------------------------------ authentification
def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **kw):
        if not store.cfg["auth"]["password_hash"]:
            return redirect(url_for("setup"))
        idle = float(store.cfg["security"].get("idle_days") or 0) * 86400
        if session.get("ok") and "seen" not in session:   # sessions ouvertes avant la v3.4.1
            session["seen"] = time.time()
        expired = idle and time.time() - session.get("seen", time.time()) > idle
        if not session.get("ok") or session.get("ep") != store.cfg["auth"].get("epoch") or expired:
            session.pop("ok", None)
            if expired and not request.path.startswith("/api/"):
                flash("Session expirée après une longue inactivité : reconnecte-toi.", "err")
            if request.path.startswith("/api/"):
                return jsonify(error="non connecté"), 401
            return redirect(url_for("login", next=request.path))
        if time.time() - session.get("seen", time.time()) > 3600:   # mis a jour au plus une fois par heure
            session["seen"] = time.time()
        return f(*a, **kw)
    return wrap


def new_epoch():
    store.cfg["auth"]["epoch"] = secrets.token_urlsafe(16)


def safe_next(nxt):
    """Redirection apres connexion : uniquement vers une page de cette application."""
    from urllib.parse import urlparse
    nxt = (nxt or "/").replace("\\", "/")
    u = urlparse(nxt)
    if not nxt.startswith("/") or nxt.startswith("//") or u.scheme or u.netloc:
        return "/"
    return nxt


LOG_RE = re.compile(r"^run_\d{8}_\d{6}\.log$")


SECRET_RETYPE = ("Adresse différente de celle enregistrée : retape le mot de passe / la clé "
                 "(par sécurité, le secret enregistré n'est jamais envoyé à une autre adresse).")


def same_target(a, b):
    return (a or "").strip().rstrip("/").lower() == (b or "").strip().rstrip("/").lower()


# ------------------------------------------------------------------ sauvegardes quotidiennes
BACKUP_RE = re.compile(r"^(config|state)-\d{8}\.json$")


# ------------------------------------------------------------------ journal des corrections (avec annulation)
AUDIT_LABEL = {"ignore": "Ignoré", "unignore": "Ré-autorisé", "link": "Lien manuel", "unlink": "Lien refusé",
               "reset": "Corrections effacées", "rerequest": "Redemandé", "undo": "Annulation",
               "password": "Sécurité", "sessions": "Sécurité", "playlist": "Playlist", "export": "Export"}


ND_EMPTY = dict(nd_id=None, nd_artist=None, nd_title=None, nd_album=None, nd_duration=None, nd_path=None)


# ------------------------------------------------------------------ sauvegarde / restauration
SECRET_PATHS = [("navidrome", "password"), ("soulsync", "api_key"), ("mqtt", "password")]


# ------------------------------------------------------------------ API externe (Home Assistant)
def _key_hash(key):
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


def ext_auth(f):
    @functools.wraps(f)
    def wrap(*a, **kw):
        h = store.cfg["security"].get("api_key_hash")
        sent = request.headers.get("X-API-Key") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not h or not sent or not hmac.compare_digest(_key_hash(sent), h):
            return jsonify(ok=False, error="clé API invalide"), 401
        return f(*a, **kw)
    return wrap
