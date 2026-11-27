"""v3.4.2 : un test par correctif de l'audit de code."""
import io
import json
import os
import time

from app import clients, export, main, sync
from app.store import AUTH_F, BACKUP_DIR, DEFAULT_CFG, validate_cfg
from fakes import SS_KEY


# 04 -------------------------------------------------------------- playlist personnelle jamais ecrasee
def test_personal_playlist_with_same_name_is_not_touched(env):
    env.svc.pls["perso"] = {"name": "Voiture", "owner": "admin", "songs": ["s4"], "comment": "ma playlist"}
    env.settings()
    env.add_playlist("PLaaa", nd_name="Voiture")
    out = env.run()
    assert env.svc.pls["perso"]["songs"] == ["s4"] and env.svc.pls["perso"]["comment"] == "ma playlist"
    assert any("pas été créée par YTM Sync" in x for x in out)


def test_managed_playlist_with_same_name_is_adopted(env):
    env.svc.pls["old"] = {"name": "Voiture", "owner": "admin", "songs": ["s4"],
                          "comment": clients.Navidrome.SIGNATURE}           # creee par YTM Sync (etat perdu)
    env.settings()
    env.add_playlist("PLaaa", nd_name="Voiture")
    env.run()
    assert env.svc.pls["old"]["songs"] == ["s1", "s2", "s3"] and len(env.svc.pls) == 1


# 03 -------------------------------------------------------------- heure de synchro invalide
def test_invalid_daily_time_is_refused(env):
    r = env.settings(sch_enabled="on", sch_mode="daily", sch_daily="25:00")
    assert "Heure de synchro invalide" in r.get_data(as_text=True)
    assert main.store.cfg["schedule"]["daily_time"] == "03:00" and not main.store.cfg["schedule"]["enabled"]
    assert env.get("/api/status").status_code == 200


def test_invalid_daily_time_never_crashes():
    sch = {"enabled": True, "mode": "daily", "daily_time": "25:00"}
    assert sync.next_run_ts(sch, time.time(), time.time())                 # repli sur 03:00
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    cfg["schedule"]["daily_time"] = "3h"
    assert "schedule.daily_time" in validate_cfg(cfg) and cfg["schedule"]["daily_time"] == "03:00"


# 01 / 02 ---------------------------------------------------------- jamais bloque « en cours »
def test_sync_runs_without_log_file(env, monkeypatch, tmp_path):
    env.settings()
    env.add_playlist("PLaaa")
    blocker = tmp_path / "fichier"
    blocker.write_text("x")
    monkeypatch.setattr(sync, "LOG_DIR", str(blocker / "logs"))           # dossier de journaux impossible
    out = env.run()
    assert any("Journal impossible" in x for x in out) and not main.runner.running
    assert env.nd_songs()                                                  # la synchro a bien eu lieu


def test_sync_released_when_state_cannot_be_saved(env, monkeypatch):
    env.settings()
    env.add_playlist("PLaaa")
    monkeypatch.setattr(main.store, "save_state", lambda: (_ for _ in ()).throw(OSError("disque plein")))
    out = env.run()
    assert not main.runner.running and any("disque plein" in x for x in out)


def test_export_lock_released_when_state_cannot_be_saved(env, monkeypatch):
    open(AUTH_F, "w").write("{}")
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    ndp = env.get("/api/navidrome/playlists").get_json()["playlists"][0]
    monkeypatch.setattr(main.store, "save_state", lambda: (_ for _ in ()).throw(OSError("disque plein")))
    export.export_to_ytm(main.store, main.runner.log, ndp["id"], "X")
    assert export.EXPORT_LOCK.acquire(blocking=False)                      # le verrou a ete libere
    export.EXPORT_LOCK.release()


# 05 -------------------------------------------------------------- secret jamais envoye a une autre adresse
def test_secret_cleared_when_address_changes(env):
    env.settings()
    assert env.api("/api/accounts", {"action": "add", "user": "barbara", "password": "pw-b"})["ok"]
    r = env.settings(nd_url="http://autre-serveur:4533", nd_pass="••••••••", ss_key="••••••••")
    text = r.get_data(as_text=True)
    assert "Ressaisis-le pour : Navidrome, 1 compte(s)" in text
    c = main.store.cfg
    assert c["navidrome"]["password"] == "" and c["nd_accounts"][0]["password"] == ""
    assert c["soulsync"]["api_key"] == SS_KEY                               # adresse inchangee : garde
    env.settings(nd_url="http://autre-serveur:4533", nd_pass="nouveau")    # ressaisi : accepte
    assert main.store.cfg["navidrome"]["password"] == "nouveau"


def test_restore_does_not_reuse_secret_for_other_address(env):
    env.settings()
    bk = env.get("/backup.json").get_json()
    bk["config"]["navidrome"]["url"] = "http://ailleurs:4533"
    r = env.c.post("/restore", data={"_csrf": env.csrf, "file": (io.BytesIO(json.dumps(bk).encode()), "b.json")},
                   content_type="multipart/form-data", follow_redirects=True)
    assert "secret à ressaisir pour Navidrome" in r.get_data(as_text=True)
    assert main.store.cfg["navidrome"]["password"] == "" and main.store.cfg["soulsync"]["api_key"] == SS_KEY


# 06 -------------------------------------------------------------- restauration tout ou rien
def test_daily_restore_with_bad_state_changes_nothing(env):
    env.settings()
    env.add_playlist("PLaaa")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    cfg = json.loads(json.dumps(main.store.cfg))
    cfg["playlists"] = []
    json.dump(cfg, open(os.path.join(BACKUP_DIR, "config-20260101.json"), "w"))
    open(os.path.join(BACKUP_DIR, "state-20260101.json"), "w").write("{abîmé")
    r = env.api("/api/backups/20260101/restore", {"with_state": True})
    assert not r["ok"] and "rien n'a été modifié" in r["msg"]
    assert len(main.store.cfg["playlists"]) == 1


# 09 -------------------------------------------------------------- pas de restauration pendant une synchro
def test_restore_refused_during_sync(env):
    main.runner.running = True
    try:
        r = env.api("/api/backups/20260101/restore", {})
        assert not r["ok"] and "en cours" in r["msg"]
    finally:
        main.runner.running = False


# 07 -------------------------------------------------------------- session YouTube conservee
def test_invalid_headers_keep_previous_youtube_session(env, monkeypatch):
    import ytmusicapi
    open(AUTH_F, "w").write('{"session": "valide"}')

    class Refused:
        def get_account_info(self):
            raise Exception("401")

        def get_library_playlists(self, limit=None):
            raise Exception("401")

    monkeypatch.setattr(ytmusicapi, "setup", lambda filepath, headers_raw: open(filepath, "w").write("{}"))
    monkeypatch.setattr(clients, "_ytm_from", lambda path: Refused())
    r = env.form("/youtube", action="headers", headers="cookie: expiré")
    assert "session précédente est conservée" in r.get_data(as_text=True)
    assert json.load(open(AUTH_F)) == {"session": "valide"} and not os.path.exists(AUTH_F + ".new")


# 08 -------------------------------------------------------------- en-tetes de proxy
def _seen_ip(peer, ips=""):
    main.store.cfg["security"].update(behind_proxy=True, proxy_ips=ips)
    got = {}

    def app(environ, start):
        got["ip"] = environ["REMOTE_ADDR"]
        return []
    main._ProxyAware(app)({"REMOTE_ADDR": peer, "HTTP_X_FORWARDED_FOR": "6.6.6.6", "wsgi.url_scheme": "http",
                           "HTTP_HOST": "x", "SERVER_NAME": "x", "SERVER_PORT": "80"}, None)
    return got["ip"]


def test_proxy_headers_trusted_only_from_local_or_listed(env):
    assert _seen_ip("172.17.0.1") == "6.6.6.6"            # liste vide : proxy du reseau local/Docker cru
    assert _seen_ip("81.240.1.2") == "81.240.1.2"         # liste vide : adresse Internet jamais crue
    assert _seen_ip("192.168.5.20", "192.168.5.10") == "192.168.5.20"   # liste renseignee : elle seule
    assert _seen_ip("192.168.5.10", "192.168.5.10") == "6.6.6.6"
    main.store.cfg["security"].update(behind_proxy=False, proxy_ips="")


# 10 -------------------------------------------------------------- healthcheck
def test_healthz_reports_stuck_scheduler(env, monkeypatch):
    assert env.get("/healthz").get_json() == {"ok": True}
    monkeypatch.setitem(sync.HEARTBEAT, "scheduler", time.time() - 3600)
    r = env.get("/healthz")
    assert r.status_code == 503 and "planificateur bloqué" in r.get_json()["problems"]


# 14 -------------------------------------------------------------- negociation gzip
def test_gzip_respects_q0(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    r = env.c.get("/settings", headers={"Accept-Encoding": "gzip;q=0, identity"})
    assert "Content-Encoding" not in r.headers and "Accept-Encoding" in r.headers.get("Vary", "")
    assert env.c.get("/settings", headers={"Accept-Encoding": "gzip"}).headers.get("Content-Encoding") == "gzip"


# 15 -------------------------------------------------------------- export sans empreinte de cle API
def test_export_without_api_key_hash_and_restore_keeps_key(env):
    env.settings()
    key = env.api("/api/apikey", {"action": "generate"})["key"]
    bk = env.get("/backup.json").get_json()
    assert not bk["config"]["security"]["api_key_hash"]
    env.c.post("/restore", data={"_csrf": env.csrf, "file": (io.BytesIO(json.dumps(bk).encode()), "b.json")},
               content_type="multipart/form-data")
    assert env.c.get("/api/ext/status", headers={"X-API-Key": key}).status_code == 200


# arret propre ------------------------------------------------------
def test_sigterm_saves_state(monkeypatch):
    saved, code = [], []
    monkeypatch.setattr(main.store, "save_state", lambda: saved.append(1))
    monkeypatch.setattr(main.os, "_exit", lambda c: code.append(c))
    main._shutdown(15, None)
    main.store.lock.release()
    assert saved == [1] and code == [0]
