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


VERSION_TAGS = {
    "live": re.compile(r"\b(live|en concert|en direct|in concert)\b"),
    "remix": re.compile(r"\b(remix|rmx|remixed|bootleg|rework)\b"),
    "acoustic": re.compile(r"\b(acoustic|acoustique|unplugged|acustico)\b"),
    "instrumental": re.compile(r"\b(instrumental|karaoke)\b"),
}


def version_tags(*texts):
    """Versions particulieres detectees dans un titre (et eventuellement un album)."""
    t = unicodedata.normalize("NFKD", " ".join(x or "" for x in texts)).encode("ascii", "ignore").decode().lower()
    return frozenset(k for k, rx in VERSION_TAGS.items() if rx.search(t))


def song_key(artist, title):
    """Identifiant d'un morceau independant de la video (clip / audio)."""
    return f"{norm(artist)}|{norm(title)}|{','.join(sorted(version_tags(title)))}"


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


# ------------------------------------------------------------------ correspondance
def _sim_n(a, b):
    """Similarite entre deux chaines DEJA normalisees."""
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    sm = SequenceMatcher(None, a, b)
    return sm.ratio() if sm.real_quick_ratio() > 0.3 else 0.0


def best_match(cands, title, artist, t_th, a_th, album=None, duration=0, strict_versions=True, blocked=()):
    """Meilleur candidat Navidrome pour un titre YouTube, ou None.
    - versions : un titre Live/Remix/Acoustique/Instrumental ne correspond qu'a la meme version
    - duree    : departage les candidats (le plus proche gagne) ; jamais de rejet, car SoulSync
                 cherche par nom et retelechargerait la meme version"""
    want = version_tags(title, album) if album else version_tags(title)
    nt, na = norm(title), norm(artist)
    best, best_score, seen = None, -1.0, set()
    for s in cands:
        if s["id"] in seen or s["id"] in blocked:
            continue
        seen.add(s["id"])
        ts = _sim_n(nt, s.get("_nt") or norm(s.get("title")))
        if ts < t_th:
            continue
        ars = max(_sim_n(na, s.get("_na") or norm(s.get("artist"))),
                  _sim_n(na, s.get("_naa") or norm(s.get("albumArtist") or s.get("displayAlbumArtist"))))
        if ars < a_th:
            continue
        if strict_versions:
            got = version_tags(s.get("title"), s.get("album")) if album else version_tags(s.get("title"))
            if got != want:
                continue
        score = ts + ars
        if duration and s.get("duration"):
            score -= min(abs(duration - s["duration"]), 120) / 120
        if score > best_score:
            best, best_score = s, score
    return best


def parse_rules(text):
    """Regles d'exclusion : une par ligne. 'artiste: X', 'album: X', 'titre: X' ou juste un mot-cle (titre)."""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        field, val = "title", line
        m = re.match(r"^(artiste|artist|album|titre|title)\s*:\s*(.+)$", line, re.I)
        if m:
            field = {"artiste": "artist", "artist": "artist", "album": "album"}.get(m.group(1).lower(), "title")
            val = m.group(2)
        # pas de norm() : il retirerait justement des mots comme "live" ou "remaster"
        v = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", unicodedata.normalize("NFKD", val)
                                       .encode("ascii", "ignore").decode().lower())).strip()
        if v:
            rules.append((field, v, line))
    return rules


def match_rule(rules, title, artist, album):
    def flat(x):
        x = unicodedata.normalize("NFKD", x or "").encode("ascii", "ignore").decode().lower()
        return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", x)).strip() + " "
    vals = {"title": flat(title), "artist": flat(artist), "album": flat(album)}
    for field, v, raw in rules:
        if f" {v} " in vals[field]:
            return raw
    return None


# ------------------------------------------------------------------ Navidrome
class Navidrome:
    def __init__(self, url, user, password):
        self.url, self.user, self.pw = (url or "").rstrip("/"), user, password or ""

    def call(self, endpoint, _data=None, **p):
        salt = secrets.token_hex(6)
        p.update(u=self.user, s=salt, v="1.16.1", c="ytm-sync", f="json",
                 t=hashlib.md5((self.pw + salt).encode()).hexdigest())
        url = f"{self.url}/rest/{endpoint}"
        if _data is None:
            r = requests.get(url, params=p, timeout=60)
        else:
            r = requests.post(url, data=list(p.items()) + _data, timeout=60)
        r.raise_for_status()
        j = r.json()["subsonic-response"]
        if j.get("status") != "ok":
            raise RuntimeError((j.get("error") or {}).get("message", "erreur Subsonic"))
        return j

    def ping(self):
        return self.call("ping").get("serverVersion", "ok")

    def get_song(self, song_id):
        return self.call("getSong", id=song_id).get("song")

    def search_songs(self, q, n=30, offset=0):
        return self.call("search3", query=q, songCount=n, songOffset=offset, albumCount=0, artistCount=0) \
            .get("searchResult3", {}).get("song", [])

    def all_songs(self, page=500):
        """Toute la bibliotheque (search3 avec requete vide, supporte par Navidrome)."""
        out, off = [], 0
        while True:
            chunk = self.search_songs("", page, off)
            out.extend(chunk)
            if len(chunk) < page:
                return out
            off += page

    def find(self, title, artist, t_th, a_th, **kw):
        """Recherche via l'API (sans cache)."""
        for q in (f"{norm(artist)} {norm(title)}", norm(title)):
            if q.strip():
                hit = best_match(self.search_songs(q, 40), title, artist, t_th, a_th, **kw)
                if hit:
                    return hit
        return None

    def owned_playlists(self):
        pls = self.call("getPlaylists").get("playlists", {}).get("playlist", [])
        return {p["name"]: p["id"] for p in pls if p.get("owner") == self.user}

    def sync_playlist(self, name, song_ids, existing, public, dry):
        """Retourne (texte, modifiee?)."""
        data = [("songId", i) for i in song_ids]
        pid = existing.get(name)
        if pid:
            cur = [e["id"] for e in self.call("getPlaylist", id=pid)["playlist"].get("entry", [])]
            if cur == song_ids:
                return "inchangée", False
            if dry:
                return f"serait mise à jour ({len(cur)} → {len(song_ids)})", False
            self.call("createPlaylist", _data=data, playlistId=pid)
            res = f"mise à jour ({len(cur)} → {len(song_ids)})"
        else:
            if dry:
                return f"serait créée ({len(song_ids)} titres)", False
            pid = self.call("createPlaylist", _data=data, name=name)["playlist"]["id"]
            existing[name] = pid
            res = f"créée ({len(song_ids)} titres)"
        self.call("updatePlaylist", playlistId=pid, public=str(bool(public)).lower(),
                  comment="Synchro auto YTM Sync - ne pas modifier à la main")
        return res, True

    def scan(self, log, timeout=900):
        self.call("startScan")
        t0 = time.time()
        while time.time() - t0 < timeout:
            time.sleep(5)
            st = self.call("getScanStatus").get("scanStatus", {})
            if not st.get("scanning"):
                log(f"Scan Navidrome terminé ({st.get('count', '?')} fichiers)")
                return True
        log("Scan Navidrome : délai dépassé, on continue")
        return False


class Library:
    """Bibliotheque Navidrome chargee en memoire une fois par synchro (bien plus rapide que l'API)."""

    def __init__(self, nd):
        t0 = time.time()
        self.songs = nd.all_songs()
        self.by_title = collections.defaultdict(list)
        self.by_artist = collections.defaultdict(list)
        for s in self.songs:
            s["_nt"] = norm(s.get("title"))
            s["_na"] = norm(s.get("artist"))
            s["_naa"] = norm(s.get("albumArtist") or s.get("displayAlbumArtist"))
            self.by_title[s["_nt"]].append(s)
            self.by_artist[s["_na"]].append(s)
            if s["_naa"] and s["_naa"] != s["_na"]:
                self.by_artist[s["_naa"]].append(s)
        self._artists = list(self.by_artist)
        self._acache = {}
        self.load_time = time.time() - t0

    def _artist_keys(self, na, a_th):
        k = (na, a_th)
        if k not in self._acache:
            self._acache[k] = [x for x in self._artists if _sim_n(na, x) >= a_th] if a_th > 0 else self._artists
        return self._acache[k]

    def find(self, title, artist, t_th, a_th, **kw):
        cands = list(self.by_title.get(norm(title), []))
        for ak in self._artist_keys(norm(artist), a_th):
            cands.extend(self.by_artist[ak])
        return best_match(cands, title, artist, t_th, a_th, **kw)


# ------------------------------------------------------------------ SoulSync
class SoulSync:
    WAIT_REASON = "Absent de Navidrome (YTM Sync)"

    def __init__(self, url, key, profile):
        self.base = (url or "").strip().rstrip("/") + "/api/v1"
        self.h = {"Authorization": f"Bearer {(key or '').strip()}", "X-Profile-Id": str(profile or "1").strip()}

    @staticmethod
    def _check(r):
        if r.status_code >= 400:
            try:
                j = r.json()
                detail = j.get("error") or j.get("message") or j
                if isinstance(detail, dict):
                    detail = " - ".join(str(v) for v in detail.values())
            except ValueError:
                detail = r.text[:200]
            raise RuntimeError(f"SoulSync HTTP {r.status_code} : {detail}")

    def downloads(self):
        r = requests.get(f"{self.base}/downloads", headers=self.h, timeout=20)
        self._check(r)
        return (r.json().get("data") or {}).get("downloads", []) or []

    def wishlist(self):
        out, page = [], 1
        while page < 100:
            r = requests.get(f"{self.base}/wishlist", headers=self.h, timeout=20,
                             params={"page": page, "limit": 200})
            self._check(r)
            j = r.json()
            items = (j.get("data") or {}).get("tracks", []) or []
            out.extend(items)
            pg = j.get("pagination") or {}
            if len(items) < 200 or (pg.get("total_pages") and page >= pg["total_pages"]):
                return out
            page += 1
        return out

    def delete_wishlist(self, track_id):
        r = requests.delete(f"{self.base}/wishlist/{track_id}", headers=self.h, timeout=20)
        return r.status_code

    def add(self, t):
        body = {"spotify_track_data": {
            "id": f"ytm_{t['videoId']}", "name": t["title"],
            "artists": [{"name": a} for a in t["artists"]] or [{"name": "Unknown"}],
            "album": {"name": t["album"] or t["title"], "album_type": "album" if t["album"] else "single"},
            "duration_ms": t["duration_ms"]},
            "failure_reason": self.WAIT_REASON, "source_type": "api"}
        r = requests.post(f"{self.base}/wishlist", json=body, headers=self.h, timeout=30)
        return r.status_code, r.text[:200]

    def process(self):
        return requests.post(f"{self.base}/wishlist/process", headers=self.h, timeout=30).status_code


def soulsync_from(cfg):
    c = cfg["soulsync"]
    return SoulSync(c["url"], c["api_key"], c["profile_id"]) if c["url"] and c["api_key"] else None


def nd_for(cfg, account_id=""):
    """Client Navidrome du compte principal ou d'un compte supplementaire."""
    c = cfg["navidrome"]
    if account_id:
        a = next((x for x in cfg.get("nd_accounts", []) if x["id"] == account_id), None)
        if a:
            return Navidrome(c["url"], a["user"], a["password"])
    return Navidrome(c["url"], c["user"], c["password"])


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
            try:
                self._logf.write(line + "\n")
                self._logf.flush()
            except (ValueError, OSError):
                pass

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
        details = {}
        try:
            res = self._run(trigger, dry, only)
            details = res.pop("_details", {})
            summary.update(res)
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
            try:
                self._notify(summary, details)
            except Exception as ex:
                self.log(f"!! Notification : {ex}")
            self.log(f"=== Fin ({summary['status']}) ===")
            self._logf.close()
            self._logf = None
            self._prune_logs()
            self.progress = {}
            self.running = False

    def _notify(self, sm, details):
        from . import notify
        cfg = self.store.cfg
        mode = cfg["notify"].get("on_sync", "changes")
        if sm.get("dry") or mode == "never":
            return
        err = sm["status"] == "erreur" or sm.get("errors")
        changed = sm.get("sent") or sm.get("pl_changed")
        if mode == "errors" and not err or mode == "changes" and not (err or changed):
            return
        lines = [f"{sm.get('present', 0)}/{sm.get('total', 0)} présents · {sm.get('missing', 0)} manquants · "
                 f"{sm.get('sent', 0)} envoyés à SoulSync · {sm.get('pl_changed', 0)} playlist(s) modifiée(s)"]
        if sm["status"] == "erreur":
            lines.append(f"ERREUR : {sm.get('message')}")
        if sm.get("errors"):
            lines.append(f"{sm['errors']} erreur(s) pendant la synchro")
        if sm.get("warns"):
            lines.append(f"{sm['warns']} durée(s) à vérifier")
        sent = details.get("sent", [])
        if sent:
            lines.append("\nDemandés à SoulSync :")
            lines += [f"• {x}" for x in sent[:20]] + ([f"… et {len(sent) - 20} autres"] if len(sent) > 20 else [])
        title = "YTM Sync — " + ("erreur de synchro" if sm["status"] == "erreur" else "synchro terminée")
        notify.send(cfg, title, "\n".join(lines), "high" if err else "normal", log=self.log)

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
        dur_tol = float(s.get("duration_tolerance", 15))
        strict_versions = bool(s.get("strict_versions", True))
        rules = parse_rules(s.get("exclude_rules", ""))
        now = time.time()
        self.log(f"=== YTM Sync v{VERSION} — {trigger} — {'SIMULATION' if DRY else 'RÉEL'} ===")

        nd = nd_for(cfg)
        self.log(f"Navidrome OK (v{nd.ping()})")
        if s["scan_before"]:
            self.log("Scan Navidrome en cours…")
            nd.scan(self.log)
        searcher = nd
        if s.get("library_cache", True):
            try:
                lib = Library(nd)
                if lib.songs:
                    searcher = lib
                    self.log(f"Bibliothèque chargée : {len(lib.songs)} morceaux en {lib.load_time:.1f} s")
                else:
                    self.log("Bibliothèque vide via le cache : recherche par l'API")
            except Exception as ex:
                self.log(f"?? Cache bibliothèque indisponible ({ex}) : recherche par l'API")
        ss = soulsync_from(cfg)
        if not ss:
            self.log("SoulSync non configuré : les manquants seront seulement listés")
        yt = ytm_client()
        self.log("YouTube Music : " + ("mode connecté" if os.path.exists(AUTH_F) else "mode anonyme (playlists publiques)"))

        old = self.store.state.get("tracks", {})
        tracks = {} if only is None else copy.deepcopy(old)
        downloaded = {}
        st = dict(total=0, present=0, missing=0, sent=0, pending=0, errors=0, warns=0, ignored=0, pl_changed=0)
        sent_labels = []
        run_id = secrets.token_hex(4)
        overrides = cfg.get("overrides", {})
        ignored = cfg.get("ignored", {})
        ignored_keys = {v.get("key"): vid for vid, v in ignored.items() if v.get("key")}
        req_keys = {}  # song_key -> derniere demande SoulSync (toutes videos confondues)
        for o in old.values():
            if o.get("requested_at") and o.get("key"):
                req_keys[o["key"]] = max(req_keys.get(o["key"], 0), o["requested_at"])
        listed = set()
        nd_clients, nd_lists = {}, {}  # par compte Navidrome

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
            acc = p.get("nd_account", "")
            self.log(f"### {pl.get('title')} ({len(items)} titres) → {nd_name}")
            song_ids, pst = [], dict(total=0, present=0, missing=0, ignored=0)
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
                first = e.get("_run") != run_id
                if first:
                    e["_run"] = run_id
                    e["key"] = song_key(artist, e["title"])
                    e["duration"] = tr.get("duration_seconds") or 0
                    prev = old.get(vid) or {}
                    e["requested_at"] = prev.get("requested_at")
                    for k in ("dl_status", "dl_progress", "dl_error", "dl_checked"):
                        e[k] = prev.get(k)
                    # --- ignore : manuel, meme morceau qu'un titre ignore, ou regle
                    reason = None
                    if vid in ignored:
                        reason = "ignoré manuellement"
                    elif e["key"] in ignored_keys:
                        reason = "même morceau qu'un titre ignoré"
                    else:
                        r = match_rule(rules, e["title"], artist, e["album"])
                        if r:
                            reason = f"règle « {r} »"
                    e["ignore_reason"] = reason
                    if reason:
                        st["ignored"] += 1
                        if prev.get("status") == "requested" and ss and not DRY:
                            try:  # deja demande avant d'etre ignore : on le retire de la wishlist
                                ss.delete_wishlist(f"ytm_{vid}")
                                self.log(f"  xx retiré de la wishlist SoulSync (ignoré) : {label}")
                            except Exception:
                                pass
                        e.update(status="ignored", nd_id=None, nd_artist=None, nd_title=None, nd_album=None,
                                 nd_duration=None, nd_path=None, warn=None, error=None,
                                 requested_at=None if not DRY else e["requested_at"],
                                 dl_status=None, dl_progress=None, dl_error=None)
                    else:
                        st["total"] += 1
                        self._evaluate(e, tr, artists, label, now, DRY, ss, searcher, nd, overrides,
                                       t_th, a_th, dur_tol, strict_versions, rereq, req_keys, listed, st,
                                       sent_labels, prev, downloaded)
                if e.get("status") == "ignored":
                    pst["ignored"] += 1
                elif e.get("status") == "present":
                    pst["present"] += 1
                    if e["nd_id"] not in song_ids:
                        song_ids.append(e["nd_id"])
                else:
                    pst["missing"] += 1
            result = "désactivé"
            if npl["enabled"] and not self.cancel:
                try:
                    if acc not in nd_clients:
                        nd_clients[acc] = nd_for(cfg, acc)
                        nd_lists[acc] = nd_clients[acc].owned_playlists()
                    result, changed = nd_clients[acc].sync_playlist(nd_name, song_ids, nd_lists[acc],
                                                                    npl["public"], DRY)
                    st["pl_changed"] += int(changed)
                    if acc:
                        result += f" (compte {nd_clients[acc].user})"
                except Exception as ex:
                    result = f"ERREUR {ex}"
                    st["errors"] += 1
                self.log(f"  >> Playlist Navidrome « {nd_name} » : {result}")
            self._pl_state(p["id"], dict(title=pl.get("title"), nd_name=nd_name, nd_result=result,
                                         last_sync=now, error=None, **pst))

        for e in tracks.values():
            e.pop("_run", None)
        with self.store.lock:
            self.store.state["tracks"] = tracks
            if not DRY:
                self.store.state.setdefault("downloaded", {}).update(downloaded)
            self.store.save_state()

        if ss and st["sent"] and not DRY and s["trigger_process"]:
            code = ss.process()
            self.log(f"Traitement wishlist SoulSync : HTTP {code}" + (" (déjà en cours)" if code == 409 else ""))
        if downloaded:
            self.log(f"{len(downloaded)} titre(s) demandé(s) précédemment sont maintenant dans Navidrome")
        self.log(f"Résumé : {st['total']} titres | {st['present']} présents | {st['missing']} manquants | "
                 f"{st['sent']} envoyés | {st['pending']} en attente | {st['ignored']} ignorés | "
                 f"{st['warns']} durées à vérifier | {st['errors']} erreurs")
        return dict(dry=DRY, _details={"sent": sent_labels}, **st)

    def _evaluate(self, e, tr, artists, label, now, DRY, ss, searcher, nd, overrides, t_th, a_th, dur_tol,
                  strict_versions, rereq, req_keys, listed, st, sent_labels, prev, downloaded):
        vid, artist, dur = e["videoId"], (artists[0] if artists else ""), e["duration"]
        ov = overrides.get(vid, {})
        e["manual"] = False
        e["blocked"] = len(ov.get("blocked", []))
        try:
            hit = None
            if ov.get("nd_id"):  # lien manuel
                try:
                    hit = nd.get_song(ov["nd_id"])
                    e["manual"] = True
                except Exception:
                    self.log(f"  ?? Lien manuel introuvable dans Navidrome, recherche auto : {label}")
            if not hit:
                hit = searcher.find(e["title"], artist, t_th, a_th, album=e["album"], duration=dur,
                                    strict_versions=strict_versions, blocked=set(ov.get("blocked", [])))
        except Exception as ex:
            self.log(f"  ?? Navidrome : {label} : {ex}")
            e.update(status="error", error=str(ex))
            st["errors"] += 1
            return
        if hit:
            if prev.get("status") == "requested" or prev.get("requested_at"):
                downloaded[e["videoId"]] = {"artist": artist, "title": e["title"], "key": e["key"],
                                            "nd_id": hit["id"], "nd_path": hit.get("path"),
                                            "nd_artist": hit.get("artist"), "nd_title": hit.get("title"),
                                            "nd_album": hit.get("album"), "at": now}
            e.update(status="present", nd_id=hit["id"], requested_at=None, error=None, warn=None,
                     nd_artist=hit.get("artist"), nd_title=hit.get("title"), nd_album=hit.get("album"),
                     nd_duration=hit.get("duration"), nd_path=hit.get("path"),
                     dl_status=None, dl_progress=None, dl_error=None)
            st["present"] += 1
            nd_dur = hit.get("duration") or 0
            if not e["manual"] and dur_tol and dur and nd_dur and tr.get("videoType") == "MUVT_ATV" \
                    and abs(dur - nd_dur) > dur_tol:
                e["warn"] = f"Durée différente : YouTube {dur // 60}:{dur % 60:02d}, " \
                            f"Navidrome {nd_dur // 60}:{nd_dur % 60:02d} (autre version ?)"
                st["warns"] += 1
                self.log(f"  ~~ {label} : {e['warn']}")
            return
        st["missing"] += 1
        e.update(nd_id=None, nd_artist=None, nd_title=None, nd_album=None, nd_duration=None, nd_path=None,
                 warn=None)
        last_req = max(e["requested_at"] or 0, req_keys.get(e["key"], 0))
        if last_req and now - last_req < rereq:
            if not e["requested_at"]:
                self.log(f"  == déjà demandé (même morceau, autre vidéo) : {label}")
            e["status"] = "requested"
            e["requested_at"] = e["requested_at"] or last_req
            st["pending"] += 1
        elif e["key"] in listed:
            e["status"] = "missing"
            self.log(f"  == doublon ignoré (même morceau, autre vidéo) : {label}")
        elif DRY or not ss:
            e["status"] = "missing"
            listed.add(e["key"])
            self.log(f"  -- manquant : {label}")
        else:
            code, txt = ss.add({"videoId": e["videoId"], "title": e["title"], "artists": artists,
                                "album": e["album"], "duration_ms": dur * 1000})
            if code < 300:
                e.update(status="requested", requested_at=now, error=None, dl_status="queued",
                         dl_progress=None, dl_error=None)
                req_keys[e["key"]] = now
                st["sent"] += 1
                sent_labels.append(label)
                self.log(f"  ++ envoyé à SoulSync : {label}")
            else:
                e.update(status="error", error=f"SoulSync HTTP {code} : {txt}")
                st["errors"] += 1
                self.log(f"  !! SoulSync HTTP {code} : {label} : {txt}")

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


# ------------------------------------------------------------------ Suivi des telechargements SoulSync
DL_ACTIVE = ("pending", "searching", "downloading")
DL_LABEL = {"queued": "En file (wishlist)", "pending": "En attente", "searching": "Recherche Soulseek",
            "downloading": "Téléchargement", "completed": "Téléchargé", "failed": "Échec",
            "gone": "Sorti de la wishlist"}


class Monitor:
    """Suit les titres demandes a SoulSync ; scanne Navidrome et resynchronise quand ils arrivent."""

    def __init__(self, store, runner):
        self.store, self.runner = store, runner
        self.pending_scan = set()  # playlists a resynchroniser
        self.last_check = 0
        self.busy = False
        self._force = threading.Event()

    def log(self, msg):
        self.runner.log(f"[Suivi] {msg}")

    def loop(self):
        time.sleep(30)
        while True:
            try:
                m = self.store.cfg["monitor"]
                due = time.time() - self.last_check >= max(1, float(m.get("interval_min", 5))) * 60
                if (m.get("enabled") and due) or self._force.is_set():
                    self._force.clear()
                    self.check()
            except Exception as ex:
                self.log(f"!! {ex}")
                with self.store.lock:
                    self.store.state.setdefault("monitor", {}).update(last_error=str(ex), last_check=time.time())
            self._force.wait(20)

    def trigger(self):
        self._force.set()

    def check(self):
        if self.busy:
            return
        self.busy = True
        try:
            self._check()
        finally:
            self.busy = False
            self.last_check = time.time()

    def _check(self):
        cfg = self.store.cfg
        ss = soulsync_from(cfg)
        if not ss:
            return
        with self.store.lock:
            requested = [e for e in self.store.state.get("tracks", {}).values() if e.get("status") == "requested"]
        counts = collections.Counter()
        if requested:
            wl = {w.get("spotify_track_id"): w for w in ss.wishlist()}
            dls = {}
            for d in sorted(ss.downloads(), key=lambda d: d.get("status_change_time") or 0):
                dls[(norm(d.get("artist_name")), norm(d.get("track_name")))] = d  # le plus recent gagne
            new_done, new_fail = [], []
            with self.store.lock:
                for e in requested:
                    old = e.get("dl_status")
                    d = dls.get((norm(e.get("artist")), norm(e.get("title"))))
                    w = wl.get(f"ytm_{e['videoId']}")
                    prog, err = None, None
                    if d and d.get("status") in DL_ACTIVE + ("completed", "failed"):
                        stt = d["status"]
                        prog = d.get("progress")
                        err = d.get("error")
                        if stt == "failed" and w:  # encore dans la wishlist : SoulSync reessaiera
                            err = f"{err or 'échec'} (nouvel essai prévu)"
                    elif w:
                        reason = w.get("failure_reason")
                        if reason and reason != SoulSync.WAIT_REASON and (w.get("retry_count") or 0) > 0:
                            stt, err = "failed", f"{reason} ({w.get('retry_count')} essai(s))"
                        else:
                            stt = "queued"
                    else:
                        stt = "gone"
                    e.update(dl_status=stt, dl_progress=prog, dl_error=err, dl_checked=time.time())
                    counts[stt] += 1
                    label = f"{e.get('artist')} - {e.get('title')}"
                    if stt in ("completed", "gone") and old not in ("completed", "gone"):
                        new_done.append(label)
                        self.pending_scan.update(e.get("playlists", []))
                    if stt == "failed" and old != "failed":
                        new_fail.append(f"{label} : {err}")
                self.store.save_state()
            if new_done:
                self.log(f"{len(new_done)} téléchargement(s) terminé(s)")
            if new_fail:
                self.log(f"{len(new_fail)} échec(s) de téléchargement")
                if cfg["notify"].get("on_dl_fail"):
                    from . import notify
                    notify.send(cfg, "YTM Sync — échecs de téléchargement",
                                "\n".join(f"• {x}" for x in new_fail[:25]), "high", log=self.log)
        with self.store.lock:
            self.store.state["monitor"] = dict(last_check=time.time(), last_error=None, counts=dict(counts),
                                               pending_scan=len(self.pending_scan))
            self.store.save_state()
        # --- tout est arrive : scan Navidrome puis resynchro des playlists concernees
        active = any(counts[k] for k in DL_ACTIVE)
        m = cfg["monitor"]
        if self.pending_scan and not active and not self.runner.running and m.get("auto_scan"):
            nd = nd_for(cfg)
            self.log("Téléchargements terminés : scan de la bibliothèque Navidrome…")
            try:
                nd.scan(self.log)
            except Exception as ex:
                self.log(f"!! Scan impossible ({ex}) — le compte Navidrome doit être administrateur")
                self.pending_scan.clear()
                return
            only = list(self.pending_scan)
            self.pending_scan.clear()
            if m.get("resync_after"):
                self.runner.start(trigger="après téléchargements", only=only)
