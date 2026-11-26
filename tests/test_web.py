"""Interface web : authentification, CSRF, blocage, API externe."""
import re
import time

import pytest

from app import main, services


@pytest.fixture()
def client():
    main.app.config["TESTING"] = True
    main.FAILS.clear()
    with main.app.test_client() as c:
        yield c


def token(c, path="/login"):
    return re.search(r'name="(?:_csrf|csrf)" (?:value|content)="([^"]+)"', c.get(path).get_data(as_text=True)).group(1)


PW = "motdepasse-test-1"


def ensure_setup(c):
    if not main.store.cfg["auth"]["password_hash"]:
        main.store.cfg["auth"]["epoch"] = main.store.cfg["auth"].get("epoch") or "e0"
        main.core.SETUP_CODE = "ABCD-1234"
        c.post("/setup", data={"password": PW, "password2": PW, "code": "abcd1234", "_csrf": token(c, "/setup")})


def login(c):
    ensure_setup(c)
    c.post("/login", data={"password": PW, "_csrf": token(c)})


def logout(c):
    c.post("/logout", data={"_csrf": token(c, "/login")})


def test_pages_require_login(client):
    ensure_setup(client)
    logout(client)
    for url in ("/", "/tracks", "/settings", "/cleanup", "/logs"):
        assert client.get(url).status_code == 302
    assert client.get("/api/status").status_code == 401


def test_pages_render(client):
    login(client)
    for url in ("/", "/tracks", "/playlists", "/settings", "/cleanup", "/youtube", "/logs", "/diagnostic"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert main.VERSION in r.get_data(as_text=True)


def test_csrf_required(client):
    login(client)
    assert client.post("/api/run", json={"dry": True}).status_code == 400
    t = token(client, "/")
    assert client.post("/api/wizard/hide", json={}, headers={"X-CSRF-Token": t}).status_code == 200


def test_login_lockout(client):
    ensure_setup(client)
    logout(client)
    t = token(client)
    for _ in range(5):
        client.post("/login", data={"password": "faux", "_csrf": t})
    r = client.post("/login", data={"password": PW, "_csrf": t})
    assert "Trop de tentatives" in r.get_data(as_text=True)


def test_ext_api_key(client):
    main.store.cfg["security"]["api_key_hash"] = main._key_hash("cle-de-test")
    assert client.get("/api/ext/status").status_code == 401
    assert client.get("/api/ext/status", headers={"X-API-Key": "mauvaise"}).status_code == 401
    r = client.get("/api/ext/status", headers={"Authorization": "Bearer cle-de-test"})
    assert r.status_code == 200 and r.get_json()["version"] == main.VERSION


def test_log_path_traversal(client):
    login(client)
    assert client.get("/logs/..%2Fconfig.json").status_code == 404
    assert client.get("/logs/run_2026.log").status_code == 404


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_ignore_and_unignore_same_song(client, monkeypatch):
    login(client)
    monkeypatch.setattr(services, "soulsync_from", lambda cfg: None)
    t = token(client, "/")
    main.store.state["tracks"] = {
        "a": {"videoId": "a", "key": "k1", "status": "present", "nd_id": "s1", "nd_title": "X", "playlists": []},
        "b": {"videoId": "b", "key": "k1", "status": "requested", "requested_at": 1, "playlists": []},
    }
    r = client.post("/api/tracks/bulk", json={"ids": ["a"], "action": "ignore"}, headers={"X-CSRF-Token": t})
    assert r.status_code == 200
    tr = main.store.state["tracks"]
    assert tr["a"]["status"] == tr["b"]["status"] == "ignored"      # meme morceau, autre video
    assert tr["a"]["nd_title"] is None and tr["b"]["requested_at"] is None
    client.post("/api/tracks/bulk", json={"ids": ["a"], "action": "unignore"}, headers={"X-CSRF-Token": t})
    assert tr["a"]["status"] == tr["b"]["status"] == "missing"
    rows = client.get("/api/tracks").get_json()["rows"]
    assert {r["videoId"] for r in rows} == {"a", "b"}


# ------------------------------------------------------------------ v3.2.0
def test_setup_requires_code():
    with main.app.test_client() as c:
        saved = main.store.cfg["auth"]["password_hash"]
        main.store.cfg["auth"]["password_hash"] = ""
        main.core.SETUP_CODE = "WXYZ-9876"
        r = c.post("/setup", data={"password": PW, "password2": PW, "code": "0000-0000", "_csrf": token(c, "/setup")})
        assert "installation incorrect" in r.get_data(as_text=True)
        assert main.store.cfg["auth"]["password_hash"] == ""
        r = c.post("/setup", data={"password": "court", "password2": "court", "code": "WXYZ-9876", "_csrf": token(c, "/setup")})
        assert main.store.cfg["auth"]["password_hash"] == ""          # mot de passe trop court
        main.store.cfg["auth"]["password_hash"] = saved


def test_revoke_other_sessions():
    a, b = main.app.test_client(), main.app.test_client()
    login(a)
    login(b)
    assert a.get("/api/status").status_code == 200
    b.post("/api/sessions/revoke", json={}, headers={"X-CSRF-Token": token(b, "/")})
    assert a.get("/api/status").status_code == 401        # l'autre appareil est deconnecte
    assert b.get("/api/status").status_code == 200        # celui qui a demande reste connecte


def test_safe_next():
    assert main.safe_next("/tracks") == "/tracks"
    for bad in ("//evil.com", "/\\evil.com", "https://evil.com", "javascript:alert(1)", "evil"):
        assert main.safe_next(bad) == "/", bad


def test_csv_formula_neutralized():
    assert main.csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert main.csv_cell("@SUM(A1)") == "'@SUM(A1)"
    assert main.csv_cell("Normal") == "Normal" and main.csv_cell(None) == ""


def test_settings_rollback_on_invalid_value(client):
    login(client)
    before = main.store.cfg["navidrome"]["url"]
    client.post("/settings", data={"_csrf": token(client, "/settings"), "nd_url": "http://autre:4533",
                                   "title_threshold": "pas-un-nombre"})
    assert main.store.cfg["navidrome"]["url"] == before


def test_stored_secret_never_sent_elsewhere(client):
    login(client)
    main.store.cfg["navidrome"].update(url="http://nd:4533", user="admin", password="secret")
    main.store.cfg["soulsync"].update(url="http://ss:8008", api_key="sk_secret")
    t = token(client, "/")
    r = client.post("/api/test/navidrome", json={"url": "http://pirate:80", "user": "admin", "password": main.SECRET_MASK},
                    headers={"X-CSRF-Token": t}).get_json()
    assert not r["ok"] and "retape" in r["msg"]
    r = client.post("/api/test/soulsync", json={"url": "http://pirate:80", "api_key": ""}, headers={"X-CSRF-Token": t}).get_json()
    assert not r["ok"] and "retape" in r["msg"]


def test_clear_secret(client):
    login(client)
    main.store.cfg["notify"]["ntfy"]["token"] = "jeton"
    data = {"_csrf": token(client, "/settings"), "ntfy_token": main.CLEAR_MARK, "nd_pass": main.SECRET_MASK}
    client.post("/settings", data=data)
    assert main.store.cfg["notify"]["ntfy"]["token"] == ""


def test_audit_and_undo(client, monkeypatch):
    login(client)
    monkeypatch.setattr(services, "soulsync_from", lambda cfg: None)
    t = token(client, "/")
    main.store.state["tracks"] = {"z": {"videoId": "z", "key": "kz", "status": "missing", "title": "T", "artist": "A", "playlists": []}}
    client.post("/api/tracks/bulk", json={"ids": ["z"], "action": "ignore"}, headers={"X-CSRF-Token": t})
    assert main.store.state["tracks"]["z"]["status"] == "ignored"
    entry = client.get("/api/audit").get_json()["rows"][0]
    assert entry["action"] == "ignore" and entry["can_undo"]
    client.post(f"/api/audit/{entry['id']}/undo", json={}, headers={"X-CSRF-Token": t})
    assert main.store.state["tracks"]["z"]["status"] == "missing" and "z" not in main.store.cfg["ignored"]


def test_redact_and_navidrome_error():
    from app.engine import Navidrome, redact
    assert "deadbeef" not in redact("/rest/ping?u=a&s=ab12&t=deadbeef")
    assert "sk_live" not in redact("Authorization: Bearer sk_live_123456")
    try:
        Navidrome("http://127.0.0.1:9", "admin", "x").ping()
    except Exception as ex:
        assert "t=" not in str(ex) and "s=" not in str(ex)


def test_webhook_formats(monkeypatch):
    from app import notify
    sent = []

    class R:
        def raise_for_status(self): pass
    monkeypatch.setattr(notify.requests, "post", lambda url, json=None, **k: sent.append(json) or R())
    cfg = {"notify": {"webhook": {"enabled": True, "url": "http://x", "format": "discord"}}}
    notify.send(cfg, "Titre", "Message")
    cfg["notify"]["webhook"]["format"] = "slack"
    notify.send(cfg, "Titre", "Message")
    assert sent[0]["content"].startswith("**Titre**") and sent[1]["text"].startswith("*Titre*")


def test_csp_and_no_inline_handlers(client):
    login(client)
    for url in ("/", "/tracks", "/playlists", "/settings", "/cleanup", "/youtube", "/logs", "/diagnostic"):
        r = client.get(url)
        csp = r.headers["Content-Security-Policy"]
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        html = r.get_data(as_text=True)
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
        assert not re.search(r"\son(click|change|submit|input|keydown)=", html), url
        for tag in re.findall(r"<script[^>]*>", html):          # chaque script porte le jeton de la page
            assert f'nonce="{nonce}"' in tag, (url, tag)


def test_gzip(client):
    login(client)
    main.store.state["tracks"] = {f"v{i}": {"videoId": f"v{i}", "title": "Titre assez long " * 3, "status": "missing",
                                            "playlists": []} for i in range(200)}
    r = client.get("/api/tracks", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("Content-Encoding") == "gzip"
    import gzip, json
    assert len(json.loads(gzip.decompress(r.data))["rows"]) == 200
    assert client.get("/api/tracks").headers.get("Content-Encoding") is None


def test_daily_backup_and_restore(client):
    login(client)
    main.store.daily_backup()
    bk = client.get("/api/backups").get_json()["backups"]
    assert bk and bk[0]["files"].get("config")
    t = token(client, "/")
    main.store.cfg["logs_keep"] = 99
    r = client.post(f"/api/backups/{bk[0]['day']}/restore", json={}, headers={"X-CSRF-Token": t}).get_json()
    assert r["ok"] and main.store.cfg["logs_keep"] != 99 and main.store.cfg["auth"]["password_hash"]


def test_duplicates(monkeypatch):
    from app import engine

    class Lib:
        songs = [{"id": "a", "title": "Song", "artist": "X", "duration": 200, "suffix": "mp3", "bitRate": 192},
                 {"id": "b", "title": "Song", "artist": "X", "duration": 201, "suffix": "flac", "bitRate": 900},
                 {"id": "c", "title": "Song", "artist": "X", "album": "Live in Paris", "duration": 240},   # version live
                 {"id": "d", "title": "Song", "artist": "X", "duration": 420}]                            # version longue
    for x in Lib.songs:
        x["_nt"], x["_na"] = engine.norm(x["title"]), engine.norm(x["artist"])
    monkeypatch.setattr(engine, "get_library", lambda nd, log=None: Lib)
    groups, n = main.find_duplicates()
    assert n == 4 and len(groups) == 1
    assert [f["id"] for f in groups[0]["files"]] == ["b", "a"] and groups[0]["files"][0]["best"]


def test_api_key_hashed_and_migrated(client):
    login(client)
    main.store.cfg["security"].update(api_key="ancienne-cle-en-clair", api_key_hash="")
    main.migrate_api_key()                                        # v3.3 -> v3.4 : plus de cle en clair
    assert main.store.cfg["security"]["api_key"] == ""
    assert client.get("/api/ext/status", headers={"X-API-Key": "ancienne-cle-en-clair"}).status_code == 200
    key = client.post("/api/apikey", json={"action": "generate"}, headers={"X-CSRF-Token": token(client, "/")}).get_json()["key"]
    assert key and key not in json_dump(main.store.cfg)          # la nouvelle cle n'est stockee nulle part
    assert client.get("/api/ext/status", headers={"X-API-Key": key}).status_code == 200
    assert client.get("/api/ext/status", headers={"X-API-Key": "ancienne-cle-en-clair"}).status_code == 401


def json_dump(o):
    import json
    return json.dumps(o)


def test_trusted_proxy_only(client):
    main.store.cfg["security"].update(behind_proxy=True, proxy_ips="10.9.9.9")
    try:
        r = client.get("/healthz", headers={"X-Forwarded-For": "1.2.3.4"}, environ_base={"REMOTE_ADDR": "192.168.5.50"})
        assert r.status_code == 200
        with main.app.test_request_context("/", headers={"X-Forwarded-For": "1.2.3.4"}, environ_base={"REMOTE_ADDR": "192.168.5.50"}):
            pass
        # appel direct du middleware : en-tete ignore si l'appel ne vient pas du proxy declare
        seen = {}

        def app_(env, sr):
            seen["ip"] = env.get("REMOTE_ADDR")
            return []
        mw = main._ProxyAware(app_)
        mw({"REMOTE_ADDR": "192.168.5.50", "HTTP_X_FORWARDED_FOR": "1.2.3.4", "wsgi.url_scheme": "http"}, None)
        assert seen["ip"] == "192.168.5.50"
        mw({"REMOTE_ADDR": "10.9.9.9", "HTTP_X_FORWARDED_FOR": "1.2.3.4", "wsgi.url_scheme": "http"}, None)
        assert seen["ip"] == "1.2.3.4"
    finally:
        main.store.cfg["security"].update(behind_proxy=False, proxy_ips="")


# ------------------------------------------------------------------ v3.4.1
def test_restore_rejects_bad_types(client):
    from app.store import validate_cfg
    cfg = json_load_defaults()
    cfg.update(playlists="x", logs_keep="trente", sync=dict(cfg["sync"], dry_run="oui"),
               ignored={"v": "pas un dict"}, nd_accounts=[{"id": 1}])
    fixed = validate_cfg(cfg)
    assert cfg["playlists"] == [] and cfg["logs_keep"] == 30 and cfg["sync"]["dry_run"] is True
    assert cfg["ignored"] == {} and cfg["nd_accounts"] == [] and len(fixed) >= 5
    login(client)
    import io
    data = {"app": "ytm-sync", "config": {"playlists": "x", "schedule": "n'importe quoi"}}
    r = client.post("/restore", data={"_csrf": token(client, "/settings"),
                                      "file": (io.BytesIO(json_dump(data).encode()), "b.json")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "remplacés par les valeurs par défaut" in r.get_data(as_text=True)
    for url in ("/", "/playlists", "/settings"):          # l'interface reste utilisable
        assert client.get(url).status_code == 200


def json_load_defaults():
    import copy
    from app.store import DEFAULT_CFG
    return copy.deepcopy(DEFAULT_CFG)


def test_wrong_password_is_instant(client):
    ensure_setup(client)
    logout(client)
    t, t0 = token(client), time.time()
    client.post("/login", data={"password": "faux", "_csrf": t})
    assert time.time() - t0 < 0.5                          # plus de pause qui bloquait un fil du serveur


def test_idle_session_expires(client):
    login(client)
    assert client.get("/api/status").status_code == 200
    with client.session_transaction() as s:
        s["seen"] = time.time() - 8 * 86400                # 8 jours sans activite (limite : 7)
    assert client.get("/api/status").status_code == 401
    login(client)
    with client.session_transaction() as s:
        s.pop("seen", None)                                # session ouverte avant la mise a jour
    assert client.get("/api/status").status_code == 200   # pas deconnectee
