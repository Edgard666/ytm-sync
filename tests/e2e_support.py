"""Fixtures des tests de bout en bout (chargees par conftest.py) : faux services + navigateur connecte."""
import copy
import os
import re
import time

import pytest

from app import clients, export, main, routes_api, sync
from app.store import AUTH_F, DEFAULT_CFG, DEFAULT_STATE
from fakes import SS_KEY, FakeServices, FakeYT

PW = "motdepasse-test-1"   # meme mot de passe que test_web (etat partage)


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="session")
def svc():
    s = FakeServices()
    yield s
    s.stop()


@pytest.fixture()
def yt(monkeypatch):
    fake = FakeYT()
    for mod in (clients, sync, export, routes_api):
        monkeypatch.setattr(mod, "ytm_client", lambda: fake)
    return fake


@pytest.fixture()
def env(svc, yt):
    """Application vierge + faux services remis à zéro + navigateur connecté."""
    svc.reset()
    st = main.store
    while main.runner.running:
        time.sleep(0.05)
    with st.lock:
        auth = st.cfg["auth"]
        st.cfg.clear()
        st.cfg.update(copy.deepcopy(DEFAULT_CFG))
        st.cfg["auth"] = auth
        st.state.clear()
        st.state.update(copy.deepcopy(DEFAULT_STATE))
    clients._LIB_CACHE.clear()
    main.FAILS.clear()
    if os.path.exists(AUTH_F):
        os.remove(AUTH_F)
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        if not st.cfg["auth"]["password_hash"]:
            st.cfg["auth"]["epoch"] = "e2e"
            main.core.SETUP_CODE = "ABCD-1234"
            c.post("/setup", data={"password": PW, "password2": PW, "code": "ABCD-1234", "_csrf": _tok(c, "/setup")})
        c.post("/login", data={"password": PW, "_csrf": _tok(c, "/login")})
        yield Env(c, svc, yt)


def _tok(c, path):
    return re.search(r'name="(?:_csrf|csrf)" (?:value|content)="([^"]+)"', c.get(path).get_data(as_text=True)).group(1)


class Env:
    def __init__(self, c, svc, yt):
        self.c, self.svc, self.yt = c, svc, yt
        self.csrf = _tok(c, "/")

    # -- raccourcis
    def api(self, path, j=None):
        r = self.c.post(path, json=j or {}, headers={"X-CSRF-Token": self.csrf})
        return r.get_json()

    def get(self, path):
        return self.c.get(path)

    def form(self, path, **data):
        return self.c.post(path, data=dict(data, _csrf=self.csrf), follow_redirects=True)

    def settings(self, **over):
        f = {"nd_url": self.svc.url, "nd_user": "admin", "nd_pass": "pw", "ss_url": self.svc.url,
             "ss_key": SS_KEY, "ss_profile": "1", "trigger_process": "on", "library_cache": "on",
             "re_request_days": "7", "title_threshold": "0.85", "artist_threshold": "0.6",
             "duration_tolerance": "15", "strict_versions": "on", "exclude_rules": "artiste: Personne",
             "mon_interval": "5", "mon_scan": "on", "mon_resync": "on", "nt_on_sync": "never",
             "ndpl_enabled": "on", "ndpl_prefix": "YT - ", "sch_mode": "interval", "sch_interval": "6",
             "sch_daily": "03:00", "logs_keep": "30", "idle_days": "7"}
        f.update({k: v for k, v in over.items() if v is not None})
        for k in [k for k, v in over.items() if v is None]:
            f.pop(k, None)
        r = self.form("/settings", **f)
        assert r.status_code == 200
        return r

    def add_playlist(self, source, **kw):
        return self.form("/playlists", action="add", source=source, **kw)

    def run(self, dry=False, **kw):
        """Lance une synchro et attend la fin ; renvoie les lignes de journal de ce passage."""
        lines = main.runner.logs_since(0)
        n = lines[-1]["n"] if lines else 0
        assert self.api("/api/run", dict(dry=dry, **kw))["ok"]
        for _ in range(400):
            if not main.runner.running:
                break
            time.sleep(0.05)
        assert not main.runner.running, "synchro bloquée"
        return [x["t"] for x in main.runner.logs_since(n)]

    def check(self):
        main.monitor.check()

    def track(self, vid):
        return main.store.state["tracks"][vid]

    def nd_songs(self):
        return {v["name"]: v["songs"] for v in self.svc.pls.values()}

    def wait_notes(self, n=1):
        for _ in range(100):
            if len(self.svc.notes) >= n:
                break
            time.sleep(0.05)
        return self.svc.notes


