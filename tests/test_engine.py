"""Moteur de synchro avec de faux YouTube / Navidrome / SoulSync."""
import pytest

from app import engine, sync
from app.store import Store

SONGS = [{"id": "s1", "title": "Alors on danse", "artist": "Stromae", "duration": 208, "suffix": "mp3", "bitRate": 128}]


class FakeND:
    def __init__(self, *a, **k):
        self.user = "admin"
        self.synced = {}

    def ping(self): return "test"
    def fingerprint(self): return None
    def all_songs(self): return [dict(s) for s in SONGS]
    def search_songs(self, q, n=30, offset=0): return [dict(s) for s in SONGS][offset:offset + n]
    def owned_playlists(self): return {}
    def get_song(self, i): return next(s for s in SONGS if s["id"] == i)

    def call(self, ep, _data=None, **k):
        if ep == "getStarred2":
            return {"starred2": {"song": [{"id": i} for i in FakeND.stars]}}
        if ep in ("star", "unstar"):
            ids = [v for _, v in _data]
            FakeND.stars = (FakeND.stars | set(ids)) if ep == "star" else (FakeND.stars - set(ids))
        return {}

    def sync_playlist(self, name, ids, existing, public, dry, known_id=None, description=None, **kw):
        FakeND.last[name] = list(ids)
        return "ok", True, "pl1"


class FakeSS:
    sent = []
    WAIT_REASON = "x"
    def add(self, t): FakeSS.sent.append(t["videoId"]); return 200, "ok"
    def process(self): return 202
    def delete_wishlist(self, i): return 200


class FakeYT:
    data = {}
    liked = []
    library = []

    def get_library_playlists(self, limit=None):
        return FakeYT.library

    def get_liked_songs(self, limit=None):
        return {"title": "Titres que j'aime", "tracks": FakeYT.liked}

    def get_playlist(self, pid, limit=None):
        v = FakeYT.data[pid]
        if isinstance(v, Exception):
            raise v
        return {"title": pid, "tracks": v}


T1 = {"videoId": "v1", "title": "Alors On Danse", "artists": [{"name": "Stromae"}]}
T2 = {"videoId": "v2", "title": "Titre absent", "artists": [{"name": "Personne"}]}


@pytest.fixture()
def runner(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "nd_for", lambda cfg, acc="": FakeND())
    monkeypatch.setattr(sync, "soulsync_from", lambda cfg: FakeSS())
    monkeypatch.setattr(sync, "ytm_client", lambda: FakeYT())
    FakeND.last, FakeSS.sent, FakeND.stars = {}, [], set()
    st = Store()
    st.state.update(tracks={}, playlists={}, history=[], downloaded={})
    st.cfg["playlists"] = [{"id": "p1", "source": "PL1", "nd_name": "Test", "enabled": True}]
    st.cfg["sync"].update(dry_run=False, library_cache=True, exclude_rules="")
    st.cfg["ignored"], st.cfg["overrides"] = {}, {}
    return engine.SyncRunner(st)


def test_sync_then_unreadable_playlist_keeps_state(runner):
    FakeYT.data = {"PL1": [T1, T2]}
    r = runner._run("test", None, None)
    assert r["present"] == 1 and r["sent"] == 1 and FakeND.last["Test"] == ["s1"]
    # bug 2 : playlist illisible -> titres conserves, pas de nouvelle demande
    FakeYT.data = {"PL1": RuntimeError("réseau")}
    FakeSS.sent = []
    runner._run("test", None, None)
    assert set(runner.store.state["tracks"]) == {"v1", "v2"} and FakeSS.sent == []


def test_empty_playlist_does_not_empty_navidrome(runner):
    FakeYT.data = {"PL1": [T1]}
    runner._run("test", None, None)
    runner.store.state["playlists"]["p1"]["total"] = 1
    FakeND.last = {}
    FakeYT.data = {"PL1": []}                       # bug 3 : vue vide (session expiree...)
    r = runner._run("test", None, None)
    assert "Test" not in FakeND.last and r["errors"] == 1
    assert "v1" in runner.store.state["tracks"]


def test_downloaded_but_unmatched_is_not_requested_again(runner):
    FakeYT.data = {"PL1": [T2]}
    runner._run("test", None, None)
    e = runner.store.state["tracks"]["v2"]
    e["dl_status"], e["requested_at"] = "gone", e["requested_at"] - 30 * 86400   # bug 5
    FakeSS.sent = []
    r = runner._run("test", None, None)
    assert FakeSS.sent == [] and r["unmatched"] == 1
    assert runner.store.state["tracks"]["v2"]["dl_status"] == "unmatched"


def test_duplicate_target_name(runner):
    runner.store.cfg["playlists"].append({"id": "p2", "source": "PL2", "nd_name": "test", "enabled": True})
    FakeYT.data = {"PL1": [T1], "PL2": [T1]}
    r = runner._run("test", None, None)
    assert r["errors"] == 1 and "nom déjà utilisé" in runner.store.state["playlists"]["p2"]["nd_result"]


def test_likes_to_stars(runner, monkeypatch, tmp_path):
    auth = tmp_path / "browser.json"
    auth.write_text("{}")
    monkeypatch.setattr(sync, "AUTH_F", str(auth))
    monkeypatch.setattr(sync, "ytm_account", lambda: "test")
    runner.store.cfg["sync"].update(star_likes=True, unstar_removed=True)
    FakeYT.data, FakeYT.liked = {"PL1": [T1]}, [T1]
    runner._run("test", None, None)
    assert FakeND.stars == {"s1"} and runner.store.state["starred_by_app"] == ["s1"]
    FakeYT.liked = []                                  # liste vide (session expiree ?) -> on ne retire RIEN
    runner._run("test", None, None)
    assert FakeND.stars == {"s1"}
    FakeYT.liked = [T2]                                # plus aime (mais liste non vide) -> retire
    runner._run("test", None, None)
    assert FakeND.stars == set()


def test_corrupted_state_is_recovered(tmp_path, monkeypatch):
    import app.store as st
    monkeypatch.setattr(st, "STATE_F", str(tmp_path / "state.json"))
    monkeypatch.setattr(st, "CFG_F", str(tmp_path / "config.json"))
    monkeypatch.setattr(st, "BACKUP_DIR", str(tmp_path / "backups"))
    s1 = st.Store()
    s1.state["tracks"] = {"v": {"x": 1}}
    s1.save_state()
    s1.save_state()                                     # cree la copie .bak
    (tmp_path / "state.json").write_text('{"tracks": {"v"')   # fichier tronque
    s2 = st.Store()
    assert s2.state["tracks"] == {"v": {"x": 1}} and s2.recovered
    assert any(p.name.startswith("state.json.abime-") for p in tmp_path.iterdir())   # l'abime est conserve


def test_quality_flag_and_auto_watch(runner, monkeypatch, tmp_path):
    auth = tmp_path / "browser.json"
    auth.write_text("{}")
    monkeypatch.setattr(sync, "AUTH_F", str(auth))
    monkeypatch.setattr(sync, "ytm_account", lambda: "test")
    runner.store.cfg["sync"]["min_bitrate"] = 192
    runner.store.cfg["auto_watch"] = {"enabled": True, "mode": "prefix", "prefix": "Navi", "dismissed": ["PLold"]}
    FakeYT.library = [{"playlistId": "PLnew", "title": "Navi - Voiture"}, {"playlistId": "PLother", "title": "Autre"},
                      {"playlistId": "PLold", "title": "Navi - retirée"}]
    FakeYT.data = {"PL1": [T1], "PLnew": [T1]}
    r = runner._run("test", None, None)
    srcs = [p["source"] for p in runner.store.cfg["playlists"]]
    assert "PLnew" in srcs and "PLother" not in srcs and "PLold" not in srcs   # prefixe + playlist ecartee
    assert runner.store.state["tracks"]["v1"]["low_quality"] == "MP3 128 kbps" and r["low_quality"] == 1


def _hook_after_first_playlist(monkeypatch, fn):
    """Execute fn(runner) juste apres le traitement de la 1re playlist (comme un clic pendant la synchro)."""
    orig = engine.SyncRunner._pl_state
    calls = []

    def hooked(self, pid, data):
        orig(self, pid, data)
        if not calls:
            calls.append(1)
            fn(self)
    monkeypatch.setattr(engine.SyncRunner, "_pl_state", hooked)


def test_cancel_keeps_unprocessed(runner, monkeypatch):
    runner.store.cfg["playlists"].append({"id": "p2", "source": "PL2", "nd_name": "Deux", "enabled": True})
    FakeYT.data = {"PL1": [T1], "PL2": [T2]}
    runner._run("test", None, None)
    assert runner.store.state["tracks"]["v2"]["requested_at"]
    _hook_after_first_playlist(monkeypatch, lambda r: setattr(r, "cancel", True))   # « Arreter »
    runner._run("test", None, None)
    assert set(runner.store.state["tracks"]) == {"v1", "v2"}                      # rien de perdu
    monkeypatch.undo()
    monkeypatch.setattr(sync, "nd_for", lambda cfg, acc="": FakeND())
    monkeypatch.setattr(sync, "soulsync_from", lambda cfg: FakeSS())
    monkeypatch.setattr(sync, "ytm_client", lambda: FakeYT())
    runner.cancel, FakeSS.sent = False, []
    runner._run("test", None, None)
    assert FakeSS.sent == []                                                         # pas de doublon


def test_ignore_during_run_is_applied(runner, monkeypatch):
    runner.store.cfg["playlists"].append({"id": "p2", "source": "PL2", "nd_name": "Deux", "enabled": True})
    FakeYT.data = {"PL1": [T1], "PL2": [T2]}
    _hook_after_first_playlist(monkeypatch, lambda r: r.store.cfg["ignored"].update(
        v1={"artist": "Stromae", "title": "Alors On Danse", "key": None}))           # clic « Ignorer » pendant la synchro
    runner._run("test", None, None)
    assert runner.store.state["tracks"]["v1"]["status"] == "ignored"


def test_schedule_catch_up():
    sch = {"enabled": True, "mode": "daily", "daily_time": "03:00"}
    import datetime as dt
    yesterday_3h = dt.datetime.now().replace(hour=3, minute=0, second=5) - dt.timedelta(days=1)
    start = yesterday_3h.timestamp() + 36 * 3600              # redemarre 36 h apres : la synchro du jour a ete manquee
    nxt = engine.next_run_ts(sch, yesterday_3h.timestamp(), start)
    assert nxt == start + engine.CATCHUP_DELAY                # rattrapee 2 min apres le demarrage
    assert engine.next_run_ts(sch, start - 60, start) > start + 3600   # rien de manque : horaire normal
    assert engine.next_run_ts({"enabled": False}, None, start) is None
