"""Faux services pour les tests de bout en bout : Navidrome (Subsonic), SoulSync, ntfy, Discord, YouTube Music.

Le faux serveur tourne dans un fil du processus de test (port choisi par le système) ;
l'application lui parle en vrai HTTP, comme en production.
"""
import copy
import threading

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

BASE_SONGS = [
    {"id": "s1", "title": "Alors on danse", "artist": "Stromae", "duration": 208, "suffix": "mp3", "bitRate": 128,
     "path": "Stromae/Cheese/02.mp3"},
    {"id": "s2", "title": "Papaoutai", "artist": "Stromae", "duration": 232, "suffix": "flac", "bitRate": 980,
     "path": "Stromae/Racine/01.flac"},
    {"id": "s2b", "title": "Papaoutai", "artist": "Stromae", "duration": 232, "suffix": "mp3", "bitRate": 192,
     "path": "Divers/papaoutai.mp3"},
    {"id": "s3", "title": "Bohemian Rhapsody", "artist": "Queen", "duration": 355},
    {"id": "s3L", "title": "Bohemian Rhapsody", "artist": "Queen", "album": "Live at Wembley '86", "duration": 330},
    {"id": "s4", "title": "Hello", "artist": "Adele", "duration": 295},
]
SS_KEY = "sk_test"


class FakeServices:
    def __init__(self):
        self.reset()
        self.app = self._build()
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reset(self):
        self.songs = copy.deepcopy(BASE_SONGS)
        self.pls, self.wish, self.calls, self.dl, self.notes = {}, [], {}, {}, []
        self.scan, self.stars = 0, set()

    def stop(self):
        self.server.shutdown()

    # -- simulations cote SoulSync
    def downloading(self, artist, title, progress=50, status="downloading"):
        self.dl[(artist, title)] = {"artist_name": artist, "track_name": title, "status": status, "progress": progress}

    def fail(self, tid):
        for w in self.wish:
            if w["spotify_track_data"]["id"] == tid:
                w["fr"], w["rc"] = "No sources found", 2

    def done(self, tid, song):
        self.wish[:] = [w for w in self.wish if w["spotify_track_data"]["id"] != tid]
        self.songs.append(song)

    def wish_ids(self):
        return [w["spotify_track_data"]["id"] for w in self.wish]

    def _build(self):
        app = Flask("fakes")
        app.logger.disabled = True
        me = self

        def ok(**k):
            return jsonify({"subsonic-response": {"status": "ok", "serverVersion": "0.53.3", **k}})

        def P(k):
            return request.values.get(k)

        @app.route("/rest/<ep>", methods=["GET", "POST"])
        def rest(ep):
            me.calls[ep] = me.calls.get(ep, 0) + 1
            if P("p") is None and P("t") is None:
                return jsonify({"subsonic-response": {"status": "failed", "error": {"code": 10, "message": "auth"}}})
            if ep == "search3":
                q = (P("query") or "").lower()
                off, n = int(P("songOffset") or 0), int(P("songCount") or 20)
                res = me.songs if not q else [s for s in me.songs if any(w in s["title"].lower() for w in q.split())]
                return ok(searchResult3={"song": res[off:off + n]})
            if ep == "getUser":
                return ok(user={"username": P("username"), "adminRole": P("username") == "admin"})
            if ep == "deletePlaylist":
                me.pls.pop(P("id"), None)
                return ok()
            if ep == "getStarred2":
                return ok(starred2={"song": [{"id": i} for i in me.stars]})
            if ep in ("star", "unstar"):
                for i in request.values.getlist("id"):
                    (me.stars.add if ep == "star" else me.stars.discard)(i)
                return ok()
            if ep == "getSong":
                m = [x for x in me.songs if x["id"] == P("id")]
                if not m:
                    return jsonify({"subsonic-response": {"status": "failed", "error": {"code": 70, "message": "nf"}}})
                return ok(song=m[0])
            if ep == "getPlaylists":
                return ok(playlists={"playlist": [{"id": k, "name": v["name"], "owner": v.get("owner")}
                                                  for k, v in me.pls.items() if v.get("owner") == P("u")]})
            if ep == "getPlaylist":
                pl = me.pls[P("id")]
                entries = [next((dict(x) for x in me.songs if x["id"] == i), {"id": i}) for i in pl["songs"]]
                return ok(playlist={"entry": entries, "comment": pl.get("comment", ""),
                                    "public": pl.get("public", False), "name": pl["name"]})
            if ep == "createPlaylist":
                pid = P("playlistId") or f"p{len(me.pls) + 1}"
                me.pls.setdefault(pid, {"name": P("name"), "owner": P("u")})["songs"] = request.values.getlist("songId")
                return ok(playlist={"id": pid})
            if ep == "updatePlaylist":
                pl = me.pls[P("playlistId")]
                for k in ("name", "comment", "public"):
                    if P(k) is not None:
                        pl[k] = P(k) == "true" if k == "public" else P(k)
                return ok()
            if ep == "startScan":
                me.scan += 1
                return ok(scanStatus={"scanning": False, "count": len(me.songs)})
            if ep == "getScanStatus":
                return ok(scanStatus={"scanning": False, "count": len(me.songs),
                                      "lastScan": f"2026-11-26T{me.scan:02d}:00:00Z"})
            return ok()

        def ss_auth():
            return request.headers.get("Authorization") == f"Bearer {SS_KEY}"

        @app.route("/api/v1/wishlist", methods=["GET", "POST"])
        def wishlist():
            if not ss_auth():
                return jsonify(error="INVALID_KEY"), 403
            if request.method == "POST":
                me.wish.append(request.json)
                return jsonify(ok=True)
            return jsonify(data={"tracks": [{
                "spotify_track_id": w["spotify_track_data"]["id"], "track_name": w["spotify_track_data"]["name"],
                "failure_reason": w.get("fr", w.get("failure_reason")), "retry_count": w.get("rc", 0)}
                for w in me.wish]}, pagination={"total_pages": 1})

        @app.route("/api/v1/wishlist/<tid>", methods=["DELETE"])
        def wish_del(tid):
            me.wish[:] = [w for w in me.wish if w["spotify_track_data"]["id"] != tid]
            return jsonify(success=True)

        @app.route("/api/v1/wishlist/process", methods=["POST"])
        def process():
            me.calls["process"] = me.calls.get("process", 0) + 1
            return "", 202

        @app.route("/api/v1/downloads")
        def downloads():
            if not ss_auth():
                return jsonify(error="INVALID_KEY"), 403
            return jsonify(data={"downloads": list(me.dl.values())})

        @app.route("/ntfy", methods=["POST"])
        def ntfy():
            me.notes.append(("ntfy", request.json))
            return jsonify(ok=True)

        @app.route("/discord", methods=["POST"])
        def discord():
            me.notes.append(("discord", {"message": request.json.get("content", "")}))
            return "", 204

        return app


def yt_track(vid, title, artist, **kw):
    return dict(videoId=vid, title=title, artists=[{"name": artist}], **kw)


class FakeYT:
    """Remplace ytmusicapi.YTMusic : playlists, favoris, mix, recherche, creation."""

    def __init__(self):
        self.empty, self.created = set(), []
        self.tracks = [
            yt_track("v1", "Alors On Danse (Official Video)", "Stromae", album=None, duration_seconds=200),
            yt_track("v2", "Papaoutai", "Stromae - Topic", album={"name": "Racine carrée"}),
            yt_track("v9", "Titre Inconnu", "Personne"),
            yt_track("v3", "Bohemian Rhapsody (Remastered 2011)", "Queen", album={"name": "A Night at the Opera"},
                     duration_seconds=354, videoType="MUVT_ATV"),
            yt_track("v4", "Bohemian Rhapsody (Live Aid 1985)", "Queen", duration_seconds=360, videoType="MUVT_OMV"),
            yt_track("v5", "Hello (Remix)", "Adele", duration_seconds=240, videoType="MUVT_ATV"),
            yt_track("v6", "Papaoutai", "Stromae", duration_seconds=400, videoType="MUVT_ATV"),
            yt_track("v9b", "Titre Inconnu (Clip officiel)", "Personne", videoType="MUVT_OMV"),
        ]

    def get_playlist(self, pid, limit=None):
        if pid.startswith("RD"):
            raise Exception("not a playlist")
        if pid in self.empty:
            return {"title": "Ma playlist " + pid, "tracks": []}
        return {"title": "Ma playlist " + pid, "description": "Desc " + pid, "tracks": copy.deepcopy(self.tracks)}

    def get_liked_songs(self, limit=None):
        return {"title": "Titres que j'aime", "tracks": [yt_track("v1", "Alors On Danse", "Stromae")]}

    def search(self, q, filter=None, limit=5):
        db = {"stromae": [yt_track("y1", "Alors On Danse", "Stromae", duration_seconds=208),
                          yt_track("y2", "Papaoutai", "Stromae", duration_seconds=232)],
              "queen": [yt_track("y3", "Bohemian Rhapsody", "Queen", duration_seconds=355)]}
        return [r for k, v in db.items() if k in q.lower() for r in v]

    def create_playlist(self, title, description, privacy_status="PRIVATE", video_ids=None):
        self.created.append((title, privacy_status, list(video_ids or [])))
        return "PLexport1"

    def get_home(self, limit=8):
        return [{"title": "Mix pour toi", "contents": [
            {"title": "Ma Supermix", "playlistId": "RDTMAKsuper", "description": "Stromae, Queen…"},
            {"title": "Une vidéo", "videoId": "zz"}]}]

    def get_watch_playlist(self, playlistId=None, limit=100):
        return {"playlist_title": "Ma Supermix",
                "tracks": [dict(yt_track("v1", "Alors On Danse", "Stromae"), length="3:28")]}

    def get_library_playlists(self, limit=None):
        return [{"playlistId": "PLlib1", "title": "Lib 1", "count": 12}]

    def get_account_info(self):
        return {"accountName": "Testeur"}
