"""Moteur : YouTube Music -> Navidrome (comparaison + playlists miroirs) -> SoulSync."""
import collections
import copy
import datetime as dt
import hashlib
import os
import re
import secrets
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse

import requests

from .store import AUTH_F, LOG_DIR, VERSION

# ------------------------------------------------------------------ utilitaires
NOISE = re.compile(r"[\(\[][^\)\]]*(official|video|audio|lyric|clip|hd|4k|remaster|visualizer|live|version|edit)[^\)\]]*[\)\]]", re.I)
FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s.*$", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = NOISE.sub(" ", s)
    s = FEAT.sub("", s)
    s = re.sub(r"\s-\s*topic$", "", s, flags=re.I)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def pl_id(x):
    x = (x or "").strip()
    if "list=" in x:
        x = parse_qs(urlparse(x).query).get("list", [x])[0]
    return x[2:] if x.startswith("VL") else x


# ------------------------------------------------------------------ YouTube Music
def ytm_client():
    from ytmusicapi import YTMusic
    return YTMusic(AUTH_F) if os.path.exists(AUTH_F) else YTMusic()


def ytm_setup_from_headers(raw):
    import ytmusicapi
    ytmusicapi.setup(filepath=AUTH_F, headers_raw=raw)


def ytm_account():
    """Retourne le nom du compte connecte, ou None."""
    if not os.path.exists(AUTH_F):
        return None
    yt = ytm_client()
    try:
        info = yt.get_account_info()
        return info.get("accountName") or info.get("channelHandle") or "connecte"
    except Exception:
        yt.get_library_playlists(limit=1)  # leve une exception si session invalide
        return "connecte"


# ------------------------------------------------------------------ Navidrome
class Navidrome:
    def __init__(self, url, user, password):
        self.url, self.user, self.pw = (url or "").rstrip("/"), user, password

    def call(self, endpoint, _data=None, **p):
        salt = secrets.token_hex(6)
        p.update(u=self.user, s=salt, v="1.16.1", c="ytm-sync", f="json",
                 t=hashlib.md5((self.pw + salt).encode()).hexdigest())
        url = f"{self.url}/rest/{endpoint}"
        if _data is None:
            r = requests.get(url, params=p, timeout=30)
        else:
            r = requests.post(url, data=list(p.items()) + _data, timeout=60)
        r.raise_for_status()
        j = r.json()["subsonic-response"]
        if j.get("status") != "ok":
            raise RuntimeError((j.get("error") or {}).get("message", "erreur Subsonic"))
        return j

    def ping(self):
        return self.call("ping").get("serverVersion", "ok")

    def find(self, title, artist, t_th, a_th):
        for q in (f"{norm(artist)} {norm(title)}", norm(title)):
            if not q.strip():
                continue
            res = self.call("search3", query=q, songCount=40, albumCount=0, artistCount=0)
            for s in res.get("searchResult3", {}).get("song", []):
                if sim(title, s.get("title")) >= t_th and \
                        max(sim(artist, s.get("artist")), sim(artist, s.get("albumArtist", ""))) >= a_th:
                    return s
        return None

    def owned_playlists(self):
        pls = self.call("getPlaylists").get("playlists", {}).get("playlist", [])
        return {p["name"]: p["id"] for p in pls if p.get("owner") == self.user}

    def sync_playlist(self, name, song_ids, existing, public, dry):
        data = [("songId", i) for i in song_ids]
        pid = existing.get(name)
        if pid:
            cur = [e["id"] for e in self.call("getPlaylist", id=pid)["playlist"].get("entry", [])]
            if cur == song_ids:
                return "inchangée"
            if dry:
                return f"serait mise à jour ({len(cur)} → {len(song_ids)})"
            self.call("createPlaylist", _data=data, playlistId=pid)
            res = f"mise à jour ({len(cur)} → {len(song_ids)})"
        else:
            if dry:
                return f"serait créée ({len(song_ids)} titres)"
            pid = self.call("createPlaylist", _data=data, name=name)["playlist"]["id"]
            existing[name] = pid
            res = f"créée ({len(song_ids)} titres)"
        self.call("updatePlaylist", playlistId=pid, public=str(bool(public)).lower(),
                  comment="Synchro auto YTM Sync - ne pas modifier à la main")
        return res

    def scan(self, log, timeout=900):
        self.call("startScan")
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(5)
            st = self.call("getScanStatus").get("scanStatus", {})
            if not st.get("scanning"):
                log(f"Scan Navidrome terminé ({st.get('count', '?')} fichiers)")
                return
        log("Scan Navidrome : délai dépassé, on continue")


# ------------------------------------------------------------------ SoulSync
class SoulSync:
    def __init__(self, url, key, profile):
        self.base = (url or "").rstrip("/") + "/api/v1"
        self.h = {"Authorization": f"Bearer {key}", "X-Profile-Id": str(profile or "1")}

    def downloads(self):
        r = requests.get(f"{self.base}/downloads", headers=self.h, timeout=20)
        r.raise_for_status()
        return (r.json().get("data") or {}).get("downloads", [])

    def add(self, t):
        body = {"spotify_track_data": {
            "id": f"ytm_{t['videoId']}", "name": t["title"],
            "artists": [{"name": a} for a in t["artists"]] or [{"name": "Unknown"}],
            "album": {"name": t["album"] or t["title"], "album_type": "album" if t["album"] else "single"},
            "duration_ms": t["duration_ms"]},
            "failure_reason": "Absent de Navidrome (YTM Sync)", "source_type": "api"}
        r = requests.post(f"{self.base}/wishlist", json=body, headers=self.h, timeout=30)
        return r.status_code, r.text[:200]

    def process(self):
        return requests.post(f"{self.base}/wishlist/process", headers=self.h, timeout=30).status_code


# ------------------------------------------------------------------ Runner
class SyncRunner:
    def __init__(self, store):
        self.store = store
        self.running = False
        self.cancel = False
        self.progress = {}
        self.lines = collections.deque(maxlen=3000)
        self.seq = 0
        self._lock = threading.Lock()
        self._logf = None
        self.started_at = time.time()

    # -- journal
    def log(self, msg):
        line = f"{dt.datetime.now():%H:%M:%S}  {msg}"
        with self._lock:
            self.seq += 1
            self.lines.append((self.seq, line))
        if self._logf:
            self._logf.write(line + "\n")
            self._logf.flush()

    def logs_since(self, since):
        with self._lock:
            return [{"n": n, "t": t} for n, t in self.lines if n > since]

    # -- lancement
    def start(self, trigger="manuel", dry=None, only=None):
        with self._lock:
            if self.running:
                return False
            self.running, self.cancel = True, False
        threading.Thread(target=self._wrap, args=(trigger, dry, only), daemon=True).start()
        return True

    def _wrap(self, trigger, dry, only):
        os.makedirs(LOG_DIR, exist_ok=True)
        fn = os.path.join(LOG_DIR, f"run_{dt.datetime.now():%Y%m%d_%H%M%S}.log")
        self._logf = open(fn, "w", encoding="utf-8")
        summary = {"start": time.time(), "trigger": trigger, "status": "ok", "message": ""}
        try:
            summary.update(self._run(trigger, dry, only))
            if self.cancel:
                summary["status"] = "annulé"
        except Exception as ex:
            summary.update(status="erreur", message=str(ex))
            self.log(f"!! ERREUR : {ex}")
        finally:
            summary["end"] = time.time()
            with self.store.lock:
                self.store.state["last_run"] = summary
                self.store.state["history"] = ([summary] + self.store.state["history"])[:30]
                self.store.save_state()
            self.log(f"=== Fin ({summary['status']}) ===")
            self._logf.close()
            self._logf = None
            self._prune_logs()
            self.progress = {}
            self.running = False

    def _prune_logs(self):
        keep = int(self.store.cfg.get("logs_keep", 30))
        files = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("run_"))
        for f in files[:-keep]:
            try:
                os.remove(os.path.join(LOG_DIR, f))
            except OSError:
                pass

    # -- coeur
    def _run(self, trigger, dry, only):
        cfg = copy.deepcopy(self.store.cfg)
        s, npl = cfg["sync"], cfg["nd_playlists"]
        DRY = s["dry_run"] if dry is None else dry
        t_th, a_th = float(s["title_threshold"]), float(s["artist_threshold"])
        rereq = float(s["re_request_days"]) * 86400
        now = time.time()
        self.log(f"=== YTM Sync v{VERSION} — {trigger} — {'SIMULATION' if DRY else 'RÉEL'} ===")

        nd = Navidrome(**cfg["navidrome"])
        self.log(f"Navidrome OK (v{nd.ping()})")
        if s["scan_before"]:
            self.log("Scan Navidrome en cours…")
            nd.scan(self.log)
        ss = SoulSync(cfg["soulsync"]["url"], cfg["soulsync"]["api_key"], cfg["soulsync"]["profile_id"]) \
            if cfg["soulsync"]["url"] and cfg["soulsync"]["api_key"] else None
        if not ss:
            self.log("SoulSync non configuré : les manquants seront seulement listés")
        yt = ytm_client()
        self.log("YouTube Music : " + ("mode connecté" if os.path.exists(AUTH_F) else "mode anonyme (playlists publiques)"))

        old = self.store.state.get("tracks", {})
        tracks = {} if only is None else copy.deepcopy(old)
        nd_lists = nd.owned_playlists() if npl["enabled"] else {}
        st = dict(total=0, present=0, missing=0, sent=0, pending=0, errors=0)
        run_id = secrets.token_hex(4)

        pls = [p for p in cfg["playlists"] if p.get("enabled", True) and (only is None or p["id"] in only)]
        for pi, p in enumerate(pls, 1):
            if self.cancel:
                break
            pid = pl_id(p["source"])
            try:
                pl = yt.get_playlist(pid, limit=None)
            except Exception as ex:
                self.log(f"!! Playlist {pid} illisible : {ex}")
                st["errors"] += 1
                self._pl_state(p["id"], {"error": str(ex), "last_sync": now})
                continue
            items = pl.get("tracks", []) or []
            nd_name = p.get("nd_name") or npl["prefix"] + (pl.get("title") or pid)
            self.log(f"### {pl.get('title')} ({len(items)} titres) → {nd_name}")
            song_ids, pst = [], dict(total=0, present=0, missing=0)
            for ti, tr in enumerate(items, 1):
                if self.cancel:
                    break
                self.progress = {"playlist": pl.get("title"), "pl_i": pi, "pl_n": len(pls), "i": ti, "n": len(items)}
                vid = tr.get("videoId")
                if not vid:
                    continue
                artists = [a["name"] for a in (tr.get("artists") or []) if a.get("name")]
                artist = artists[0] if artists else ""
                e = tracks.get(vid) or {"videoId": vid, "playlists": []}
                e.update(title=tr.get("title") or "", artist=artist, album=(tr.get("album") or {}).get("name"))
                if p["id"] not in e["playlists"]:
                    e["playlists"].append(p["id"])
                tracks[vid] = e
                pst["total"] += 1
                label = f"{artist} - {e['title']}"
                if e.get("_run") != run_id:  # pas encore evalue pendant ce passage
                    e["_run"] = run_id
                    e["requested_at"] = (old.get(vid) or {}).get("requested_at")
                    st["total"] += 1
                    try:
                        hit = nd.find(e["title"], artist, t_th, a_th)
                    except Exception as ex:
                        self.log(f"  ?? Navidrome : {label} : {ex}")
                        e["status"] = "error"
                        e["error"] = str(ex)
                        st["errors"] += 1
                        continue
                    if hit:
                        e.update(status="present", nd_id=hit["id"], requested_at=None, error=None)
                        st["present"] += 1
                    else:
                        st["missing"] += 1
                        e["nd_id"] = None
                        if e["requested_at"] and now - e["requested_at"] < rereq:
                            e["status"] = "requested"
                            st["pending"] += 1
                        elif DRY or not ss:
                            e["status"] = "missing"
                            self.log(f"  -- manquant : {label}")
                        else:
                            code, txt = ss.add({"videoId": vid, "title": e["title"], "artists": artists,
                                                "album": e["album"],
                                                "duration_ms": (tr.get("duration_seconds") or 0) * 1000})
                            if code < 300:
                                e.update(status="requested", requested_at=now, error=None)
                                st["sent"] += 1
                                self.log(f"  ++ envoyé à SoulSync : {label}")
                            else:
                                e.update(status="error", error=f"SoulSync HTTP {code} : {txt}")
                                st["errors"] += 1
                                self.log(f"  !! SoulSync HTTP {code} : {label} : {txt}")
                if e.get("status") == "present":
                    pst["present"] += 1
                    if e["nd_id"] not in song_ids:
                        song_ids.append(e["nd_id"])
                else:
                    pst["missing"] += 1
            result = "désactivé"
            if npl["enabled"] and not self.cancel:
                try:
                    result = nd.sync_playlist(nd_name, song_ids, nd_lists, npl["public"], DRY)
                except Exception as ex:
                    result = f"ERREUR {ex}"
                self.log(f"  >> Playlist Navidrome « {nd_name} » : {result}")
            self._pl_state(p["id"], dict(title=pl.get("title"), nd_name=nd_name, nd_result=result,
                                         last_sync=now, error=None, **pst))

        for e in tracks.values():
            e.pop("_run", None)
        with self.store.lock:
            self.store.state["tracks"] = tracks
            self.store.save_state()

        if ss and st["sent"] and not DRY and s["trigger_process"]:
            code = ss.process()
            self.log(f"Traitement wishlist SoulSync : HTTP {code}" + (" (déjà en cours)" if code == 409 else ""))
        self.log(f"Résumé : {st['total']} titres | {st['present']} présents | {st['missing']} manquants | "
                 f"{st['sent']} envoyés | {st['pending']} en attente | {st['errors']} erreurs")
        return dict(dry=DRY, **st)

    def _pl_state(self, pid, data):
        with self.store.lock:
            cur = self.store.state["playlists"].get(pid, {})
            cur.update(data)
            self.store.state["playlists"][pid] = cur


# ------------------------------------------------------------------ Planificateur
def next_run_ts(sch, last_start, app_start):
    if not sch.get("enabled"):
        return None
    base = max(last_start or 0, app_start)
    if sch.get("mode") == "daily":
        try:
            hh, mm = (int(x) for x in sch.get("daily_time", "03:00").split(":"))
        except ValueError:
            hh, mm = 3, 0
        b = dt.datetime.fromtimestamp(base)
        cand = b.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() <= base:
            cand += dt.timedelta(days=1)
        return cand.timestamp()
    return base + max(0.25, float(sch.get("interval_hours", 6))) * 3600


def scheduler_loop(store, runner):
    while True:
        try:
            lr = store.state.get("last_run") or {}
            nxt = next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at)
            if nxt and time.time() >= nxt and not runner.running:
                runner.start(trigger="planifié")
        except Exception as ex:  # ne jamais tuer le planificateur
            runner.log(f"!! Planificateur : {ex}")
        time.sleep(20)
