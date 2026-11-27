"""Clients des services : YouTube Music (ytmusicapi), Navidrome (API Subsonic) et sa bibliotheque en memoire, SoulSync."""
import collections
import hashlib
import os
import requests
import secrets
import threading
import time

from .matching import _artist_sim, best_match, norm
from .store import AUTH_F


# ------------------------------------------------------------------ YouTube Music
def ytm_client():
    from ytmusicapi import YTMusic
    return YTMusic(AUTH_F) if os.path.exists(AUTH_F) else YTMusic()


def _secs(length):
    try:
        parts = [int(x) for x in str(length).split(":")]
        return sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
    except ValueError:
        return 0


def watch_as_playlist(yt, pid, limit=100):
    """Un mix YouTube Music (« RD… ») lu comme une playlist."""
    wp = yt.get_watch_playlist(playlistId=pid, limit=limit)
    tracks = [dict(t, duration_seconds=t.get("duration_seconds") or _secs(t.get("length")))
              for t in wp.get("tracks", []) if t.get("videoId")]
    return {"title": wp.get("playlist_title") or wp.get("title") or f"Mix {pid[:12]}", "tracks": tracks}


def ytm_setup_from_headers(raw):
    """Enregistre une nouvelle session YouTube Music. Elle est d'abord verifiee dans un fichier temporaire :
    des en-tetes invalides ne font jamais perdre la session qui fonctionnait. Retourne le nom du compte."""
    import ytmusicapi
    tmp = AUTH_F + ".new"
    try:
        ytmusicapi.setup(filepath=tmp, headers_raw=raw)
        os.chmod(tmp, 0o600)
        name = _account_name(_ytm_from(tmp))
        os.replace(tmp, AUTH_F)
        return name
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _ytm_from(path):
    from ytmusicapi import YTMusic
    return YTMusic(path)


def _account_name(yt):
    try:
        info = yt.get_account_info()
        return info.get("accountName") or info.get("channelHandle") or "connecte"
    except Exception:
        yt.get_library_playlists(limit=1)  # leve une exception si session invalide
        return "connecte"


def ytm_account():
    """Retourne le nom du compte connecte, ou None."""
    if not os.path.exists(AUTH_F):
        return None
    return _account_name(ytm_client())


# ------------------------------------------------------------------ Navidrome
class Navidrome:
    def __init__(self, url, user, password):
        self.url, self.user, self.pw = (url or "").rstrip("/"), user, password or ""
        self.http = requests.Session()  # connexion reutilisee (keep-alive)

    def call(self, endpoint, _data=None, **p):
        salt = secrets.token_hex(6)
        p.update(u=self.user, s=salt, v="1.16.1", c="ytm-sync", f="json",
                 t=hashlib.md5((self.pw + salt).encode()).hexdigest())
        url = f"{self.url}/rest/{endpoint}"
        # Les erreurs de requests contiennent l'URL complete (avec le jeton t= et le sel s=, qui suffisent
        # a se connecter) : on les remplace par un message sans aucun parametre.
        try:
            if _data is None:
                r = self.http.get(url, params=p, timeout=60)
            else:
                r = self.http.post(url, data=list(p.items()) + _data, timeout=60)
            r.raise_for_status()
            j = r.json()["subsonic-response"]
        except requests.HTTPError as ex:
            raise RuntimeError(f"Navidrome HTTP {ex.response.status_code} sur {endpoint}") from None
        except requests.RequestException as ex:
            raise RuntimeError(f"Navidrome injoignable ({type(ex).__name__}) à {self.url}") from None
        except (ValueError, KeyError):
            raise RuntimeError(f"Réponse invalide de Navidrome sur {endpoint} (est-ce bien l'URL de Navidrome ?)") from None
        if j.get("status") != "ok":
            raise RuntimeError((j.get("error") or {}).get("message", "erreur Subsonic"))
        return j

    def ping(self):
        return self.call("ping").get("serverVersion", "ok")

    def fingerprint(self):
        """Identifie l'etat de la bibliotheque : change apres chaque scan (None si inconnu)."""
        st = self.call("getScanStatus").get("scanStatus", {})
        if st.get("scanning") or not st.get("lastScan"):
            return None
        return (self.url, self.user, st.get("count"), st.get("lastScan"))

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

    SIGNATURE = "Synchro auto YTM Sync — ne pas modifier à la main"

    def sync_playlist(self, name, song_ids, existing, public, dry, known_id=None, description=None, labels=None):
        """Rend la playlist identique a song_ids. Retourne (texte, modifiee?, id).
        known_id : playlist deja creee pour cette source -> renommee au lieu d'en creer une nouvelle."""
        self.last_diff = None
        data = [("songId", i) for i in song_ids]
        comment = ((description or "").strip()[:500] + "\n\n" if (description or "").strip() else "") + self.SIGNATURE
        pid, notes, adopted = None, [], False
        if known_id and known_id in existing.values():          # playlist deja creee par YTM Sync
            pid = known_id
            old_name = next(n for n, i in existing.items() if i == known_id)
        elif existing.get(name):                               # meme nom : a verifier avant d'y toucher
            pid, old_name, adopted = existing[name], name, True
        if pid and old_name != name and not adopted:
            if dry:
                notes.append(f"serait renommée (était « {old_name} »)")
            else:
                self.call("updatePlaylist", playlistId=pid, name=name)
                existing.pop(old_name, None)
                existing[name] = pid
                notes.append(f"renommée (était « {old_name} »)")
        if pid:
            pl = self.call("getPlaylist", id=pid)["playlist"]
            if adopted and self.SIGNATURE not in (pl.get("comment") or ""):
                raise RuntimeError(f"une playlist « {name} » qui n'a pas été créée par YTM Sync existe déjà "
                                   "dans Navidrome : elle n'est pas modifiée. Renomme-la, ou choisis un autre nom "
                                   "dans « Playlists »")
            cur = [e["id"] for e in pl.get("entry", [])]
            cur_lab = {e["id"]: f"{e.get('artist')} — {e.get('title')}" for e in pl.get("entry", [])}
            new_set, cur_set = set(song_ids), set(cur)
            self.last_diff = {"added": [labels.get(i, i) for i in song_ids if i not in cur_set] if labels else
                              [i for i in song_ids if i not in cur_set],
                              "removed": [cur_lab[i] for i in cur if i not in new_set],
                              "reordered": cur != song_ids and new_set == cur_set}
            same_songs = cur == song_ids
            same_meta = (pl.get("comment") or "") == comment and bool(pl.get("public")) == bool(public)
            if same_songs and same_meta and not notes:
                return "inchangée", False, pid
            if dry:
                return ", ".join(notes + ([] if same_songs else [f"serait mise à jour ({len(cur)} → {len(song_ids)})"])
                                 or ["description serait mise à jour"]), False, pid
            if not same_songs:
                self.call("createPlaylist", _data=data, playlistId=pid)
                notes.append(f"mise à jour ({len(cur)} → {len(song_ids)})")
            if not same_meta:
                self.call("updatePlaylist", playlistId=pid, public=str(bool(public)).lower(), comment=comment)
                if same_songs:
                    notes.append("description mise à jour")
            return ", ".join(notes), True, pid
        self.last_diff = {"added": [labels.get(i, i) for i in song_ids] if labels else list(song_ids),
                          "removed": [], "reordered": False}
        if dry:
            return f"serait créée ({len(song_ids)} titres)", False, None
        pid = self.call("createPlaylist", _data=data, name=name)["playlist"]["id"]
        existing[name] = pid
        self.call("updatePlaylist", playlistId=pid, public=str(bool(public)).lower(), comment=comment)
        return f"créée ({len(song_ids)} titres)", True, pid

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


_LIB_CACHE = {}  # bibliotheque gardee entre deux synchros tant que Navidrome n'a pas rescanne
_LIB_LOCK = threading.Lock()


def get_library(nd, log=None):
    """Bibliotheque en memoire, rechargee seulement si Navidrome a rescanne depuis."""
    with _LIB_LOCK:
        try:
            fp = nd.fingerprint()
        except Exception:
            fp = None
        cached = _LIB_CACHE.get("lib")
        if fp and cached and _LIB_CACHE.get("fp") == fp:
            if log:
                log(f"Bibliothèque en mémoire réutilisée ({len(cached.songs)} morceaux, aucun scan depuis)")
            return cached
        lib = Library(nd)
        _LIB_CACHE.update(fp=fp, lib=lib)
        if log:
            log(f"Bibliothèque chargée : {len(lib.songs)} morceaux en {lib.load_time:.1f} s")
        return lib


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
            self._acache[k] = [x for x in self._artists if _artist_sim(na, x) >= a_th] if a_th > 0 else self._artists
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
        self.http = requests.Session()

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
        r = self.http.get(f"{self.base}/downloads", headers=self.h, timeout=20)
        self._check(r)
        return (r.json().get("data") or {}).get("downloads", []) or []

    def wishlist(self):
        out, page = [], 1
        while page < 100:
            r = self.http.get(f"{self.base}/wishlist", headers=self.h, timeout=20,
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
        r = self.http.delete(f"{self.base}/wishlist/{track_id}", headers=self.h, timeout=20)
        return r.status_code

    def add(self, t):
        body = {"spotify_track_data": {
            "id": f"ytm_{t['videoId']}", "name": t["title"],
            "artists": [{"name": a} for a in t["artists"]] or [{"name": "Unknown"}],
            "album": {"name": t["album"] or t["title"], "album_type": "album" if t["album"] else "single"},
            "duration_ms": t["duration_ms"]},
            "failure_reason": self.WAIT_REASON, "source_type": "api"}
        r = self.http.post(f"{self.base}/wishlist", json=body, headers=self.h, timeout=30)
        return r.status_code, r.text[:200]

    def process(self):
        return self.http.post(f"{self.base}/wishlist/process", headers=self.h, timeout=30).status_code


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
