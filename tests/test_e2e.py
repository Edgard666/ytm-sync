"""Scénarios de bout en bout : l'application parle en HTTP à de faux Navidrome / SoulSync / ntfy.

Chaque test repart d'une configuration vierge (seul le mot de passe de l'interface est gardé).
Les fixtures (svc, yt, env) sont dans e2e_support.py.
"""
import io
import json
import re
import time

from app import main
from app.store import AUTH_F


# ------------------------------------------------------------------ scénarios
def connect_youtube():
    """Simule un compte YouTube Music connecté (le faux client ne lit pas le fichier)."""
    with open(AUTH_F, "w") as f:
        f.write("{}")


def test_sync_creates_mirror_and_requests_missing(env):
    env.settings()
    env.add_playlist("PLaaa")
    out = env.run()
    assert any("=== Fin (ok)" in x for x in out)
    st = {v: e["status"] for v, e in main.store.state["tracks"].items()}
    assert st == {"v1": "present", "v2": "present", "v3": "present", "v6": "present",
                  "v4": "requested", "v5": "requested", "v9": "ignored", "v9b": "ignored"}
    assert env.track("v2")["nd_id"] == "s2"                       # la version FLAC est préférée
    assert env.nd_songs() == {"YT - Ma playlist PLaaa": ["s1", "s2", "s3"]}
    assert env.svc.wish_ids() == ["ytm_v4", "ytm_v5"]
    assert env.svc.calls["process"] == 1                          # wishlist traitée
    assert any("Durée différente" in x for x in out)              # v6 signalé
    # 2e passage : rien n'est redemandé, bibliothèque reprise du cache (pas de relecture complète)
    searches = env.svc.calls["search3"]
    out = env.run()
    assert env.svc.wish_ids() == ["ytm_v4", "ytm_v5"]
    assert env.svc.calls["search3"] == searches
    assert env.nd_songs() == {"YT - Ma playlist PLaaa": ["s1", "s2", "s3"]}


def test_dry_run_changes_nothing(env):
    env.settings()
    env.add_playlist("PLaaa")
    out = env.run(dry=True)
    assert any("SIMULATION" in x for x in out)
    assert env.svc.pls == {} and env.svc.wish == []


def test_second_account_and_names(env):
    env.settings()
    assert env.api("/api/accounts", {"action": "add", "user": "barbara", "password": "x"})["ok"]
    acc = main.store.cfg["nd_accounts"][0]["id"]
    env.add_playlist("PLbbb", nd_account=acc, nd_name="Barbara mix")
    r = env.add_playlist("PLccc", nd_account=acc, nd_name="barbara MIX")
    assert "déjà utilisé" in r.get_data(as_text=True)               # même nom, même compte : refusé
    env.run()
    owners = {v["name"]: v["owner"] for v in env.svc.pls.values()}
    assert owners == {"Barbara mix": "barbara"}


def test_rules_preview(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    r = env.api("/api/settings/rules-preview", {"rules": "karaoke\nartiste: Personne"})
    assert r["ok"] and r["count"] == 2


def test_monitor_download_then_resync(env):
    env.settings(mon_enabled="on")
    env.add_playlist("PLaaa")
    env.run()
    env.svc.downloading("Queen", "Bohemian Rhapsody (Live Aid 1985)", 55)
    env.check()
    assert env.track("v4")["dl_status"] == "downloading" and env.track("v4")["dl_progress"] == 55
    assert env.track("v5")["dl_status"] in ("queued", "pending", "searching")
    # le téléchargement se termine : fichier ajouté à la bibliothèque, titre sorti de la wishlist
    env.svc.downloading("Queen", "Bohemian Rhapsody (Live Aid 1985)", 100, "completed")
    env.svc.done("ytm_v4", {"id": "s5", "title": "Bohemian Rhapsody (Live Aid 1985)", "artist": "Queen",
                            "duration": 360, "path": "Queen/Live/01.flac"})
    scans = env.svc.scan
    env.check()
    for _ in range(200):                                          # resynchro lancée en tâche de fond
        if env.track("v4")["status"] == "present" and not main.runner.running:
            break
        time.sleep(0.05)
    assert env.svc.scan == scans + 1                              # scan Navidrome demandé
    assert env.track("v4")["status"] == "present" and env.track("v4")["nd_id"] == "s5"
    assert "s5" in env.nd_songs()["YT - Ma playlist PLaaa"]
    assert "v4" in main.store.state["downloaded"]


def test_failed_download_is_notified(env):
    env.settings(nt_dl_fail="on", ntfy_on="on", ntfy_url=env.svc.url + "/ntfy", ntfy_topic="test")
    env.add_playlist("PLaaa")
    env.run()
    env.svc.fail("ytm_v5")
    env.check()
    assert env.track("v5")["dl_status"] == "failed"
    assert "No sources found" in env.track("v5")["dl_error"]
    notes = env.wait_notes()
    assert any("Hello" in n[1].get("message", "") for n in notes)
    assert "Échec" in env.get("/tracks").get_data(as_text=True)


def test_ignore_removes_from_wishlist(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    assert env.api("/api/tracks/v5/ignore", {"ignore": True})["ok"]
    assert "ytm_v5" not in env.svc.wish_ids()
    env.run()
    assert env.track("v5")["status"] == "ignored" and "ytm_v5" not in env.svc.wish_ids()
    env.api("/api/tracks/v5/ignore", {"ignore": False})
    env.run()
    assert env.track("v5")["status"] == "requested"


def test_empty_playlist_keeps_navidrome(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    before = env.nd_songs()
    env.yt.empty.add("PLaaa")
    out = env.run()
    assert any("vue vide" in x for x in out)
    assert env.nd_songs() == before


def test_rename_and_description(env):
    env.settings(ndpl_desc="on")
    env.add_playlist("PLaaa", nd_name="Voiture")
    env.run()
    pid = main.store.cfg["playlists"][0]["id"]
    env.form("/playlists", action="save", id=pid, nd_name="Voiture 2026", enabled="on")
    env.run()
    (pl,) = env.svc.pls.values()
    assert pl["name"] == "Voiture 2026" and "Desc PLaaa" in pl.get("comment", "")


def test_likes_become_stars(env):
    connect_youtube()
    env.settings(star_likes="on", unstar_removed="on")
    env.add_playlist("PLaaa")
    env.run()
    assert "s1" in env.svc.stars


def test_export_navidrome_playlist_to_youtube(env):
    connect_youtube()
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    ndp = env.get("/api/navidrome/playlists").get_json()["playlists"][0]
    r = env.api("/api/export-ytm", {"id": ndp["id"], "account": ndp["account"], "name": "Export test",
                                    "privacy": "UNLISTED"})
    assert r["ok"]
    for _ in range(200):
        ex = env.get("/api/navidrome/playlists").get_json()["exports"]
        if ex and ex[0]["status"] != "running":
            break
        time.sleep(0.05)
    assert ex[0]["found"] == ex[0]["total"] == 3
    assert env.yt.created == [("Export test", "UNLISTED", ["y1", "y2", "y3"])]


def test_mix_and_auto_watch(env):
    connect_youtube()
    env.settings(aw_on="on", aw_mode="all")
    mixes = env.get("/api/youtube/mixes").get_json()["mixes"]
    assert [m["id"] for m in mixes] == ["RDTMAKsuper"]
    env.form("/youtube", action="import", pl="RDTMAKsuper")
    out = env.run()
    sources = {p["source"] for p in main.store.cfg["playlists"]}
    assert sources == {"RDTMAKsuper", "PLlib1"}                   # playlist de la bibliothèque ajoutée seule
    assert any("Supermix" in x for x in out)


def test_low_quality_upgrade(env):
    env.settings(min_bitrate="192")
    env.add_playlist("PLaaa")
    env.run()
    rows = env.get("/api/tracks").get_json()["rows"]
    low = [r["videoId"] for r in rows if r.get("low_quality")]
    assert low == ["v1"]                                          # MP3 128 kbit/s
    assert env.api("/api/tracks/bulk", {"ids": low, "action": "upgrade"})["ok"]
    assert "ytm_v1" in env.svc.wish_ids()


def test_duplicates_ignore_live_versions(env):
    env.settings()
    d = env.get("/api/cleanup/duplicates").get_json()
    groups = {g["title"]: sorted(f["id"] for f in g["files"]) for g in d["groups"]}
    assert groups == {"Papaoutai": ["s2", "s2b"]}
    assert "Papaoutai" in env.get("/export/duplicates.csv").get_data(as_text=True)


def test_exports_csv(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    csv = env.get("/export/tracks.csv").get_data(as_text=True)
    assert "Alors On Danse" in csv and "Hello (Remix)" in csv


def test_backup_restore_keeps_secrets(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.add_playlist("PLbbb")
    bk = env.get("/backup.json").get_json()
    assert bk["config"]["navidrome"]["password"] == ""            # pas de secret dans la sauvegarde
    bk["config"]["playlists"] = bk["config"]["playlists"][:1]
    env.c.post("/restore", data={"_csrf": env.csrf, "file": (io.BytesIO(json.dumps(bk).encode()), "b.json")},
               content_type="multipart/form-data")
    c = main.store.cfg
    assert len(c["playlists"]) == 1 and c["navidrome"]["password"] == "pw" and c["auth"]["password_hash"]


def test_external_api(env):
    env.settings()
    key = env.api("/api/apikey", {"action": "generate"})["key"]
    assert env.c.get("/api/ext/status").status_code == 401
    r = env.c.get("/api/ext/status", headers={"X-API-Key": key}).get_json()
    assert "counts" in r
    r = env.c.post("/api/ext/sync", headers={"Authorization": "Bearer " + key}, json={"dry": True}).get_json()
    assert r["ok"]
    while main.runner.running:
        time.sleep(0.05)


def test_navidrome_down_no_token_leak(env):
    env.settings(nd_url="http://127.0.0.1:9", nd_pass="pw-test")
    env.add_playlist("PLaaa")
    out = env.run(dry=True)
    assert any("ERREUR" in x for x in out)
    assert not any(re.search(r"[?&][ts]=[0-9a-f]{6}", x) for x in out)


def test_wrong_soulsync_key_is_reported(env):
    env.settings(ss_key="sk_mauvaise")
    r = env.api("/api/test/soulsync")
    assert not r["ok"]


def test_suggest_link_and_undo(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    sug = env.get("/api/tracks/v5/suggest").get_json()["songs"]
    assert sug and sug[0]["id"] == "s4"                           # « Hello » proposé pour « Hello (Remix) »
    assert env.api("/api/tracks/v5/link", {"nd_id": "s4"})["ok"]
    assert "ytm_v5" not in env.svc.wish_ids()
    env.run()
    assert env.track("v5")["status"] == "present"
    a = env.get("/api/audit").get_json()["rows"][0]
    assert env.api(f"/api/audit/{a['id']}/undo")["ok"]
    assert "v5" not in main.store.cfg["overrides"]


def test_delete_playlist_also_on_navidrome(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    pid = main.store.cfg["playlists"][0]["id"]
    env.form("/playlists", action="delete_nd", id=pid)
    assert env.svc.pls == {} and main.store.cfg["playlists"] == []


def test_all_pages_after_sync(env):
    env.settings()
    env.add_playlist("PLaaa")
    env.run()
    for url in ("/", "/tracks", "/playlists", "/settings", "/cleanup", "/youtube", "/logs", "/diagnostic"):
        r = env.get(url)
        assert r.status_code == 200, url
        assert main.VERSION in r.get_data(as_text=True)
    checks = env.get("/api/diagnostic").get_json()["checks"]
    assert not [c for c in checks if c["status"] == "err"], checks
