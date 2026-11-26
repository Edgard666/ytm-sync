"""Export d'une playlist Navidrome vers YouTube Music."""
import datetime as dt
import os
import threading
import time

from .clients import nd_for, ytm_client
from .matching import best_match, redact
from .store import AUTH_F


# ------------------------------------------------------------------ Export Navidrome -> YouTube Music
EXPORT_LOCK = threading.Lock()


def export_to_ytm(store, log, nd_playlist_id, name, privacy="PRIVATE", account=""):
    """Cree une playlist YouTube Music a partir d'une playlist Navidrome. Retourne le resultat (dict)."""
    if not EXPORT_LOCK.acquire(blocking=False):
        raise RuntimeError("Un export est déjà en cours")
    res = {"at": time.time(), "name": name, "status": "en cours", "found": 0, "total": 0, "not_found": []}
    try:
        if not os.path.exists(AUTH_F):
            raise RuntimeError("Compte YouTube non connecté (page Compte YouTube)")
        nd = nd_for(store.cfg, account)
        pl = nd.call("getPlaylist", id=nd_playlist_id)["playlist"]
        entries = pl.get("entry", [])
        res.update(total=len(entries), nd_name=pl.get("name"))
        log(f"[Export] « {pl.get('name')} » → YouTube Music « {name} » : {len(entries)} titre(s)")
        from concurrent.futures import ThreadPoolExecutor
        local = threading.local()

        def lookup(e):
            """Recherche d'un titre sur YouTube Music (un client par fil)."""
            if not hasattr(local, "yt"):
                local.yt = ytm_client()
            title, artist = e.get("title") or "", e.get("artist") or ""
            try:
                found = local.yt.search(f"{artist} {title}", filter="songs", limit=5) or []
                cands = [{"id": r.get("videoId"), "title": r.get("title"),
                          "artist": ((r.get("artists") or [{}])[0] or {}).get("name"),
                          "duration": r.get("duration_seconds")} for r in found if r.get("videoId")]
                hit = best_match(cands, title, artist, 0.8, 0.5, duration=e.get("duration") or 0,
                                 strict_versions=True)
                return (hit and hit["id"]), None
            except Exception as ex:
                return None, f"{artist} - {title} : {ex}"

        ids, t0 = [], time.time()
        with ThreadPoolExecutor(max_workers=4) as pool:   # 4 recherches en parallele, ordre conserve
            for i, (e, (vid, err)) in enumerate(zip(entries, pool.map(lookup, entries)), 1):
                if err:
                    log(f"[Export]   ?? recherche impossible pour {err}")
                if vid and vid not in ids:
                    ids.append(vid)
                elif not vid:
                    res["not_found"].append(f"{e.get('artist') or ''} — {e.get('title') or ''}")
                if i % 50 == 0:
                    log(f"[Export]   {i}/{len(entries)} titres traités")
        log(f"[Export]   recherche terminée en {time.time() - t0:.0f} s")
        yt = ytm_client()
        if not ids:
            raise RuntimeError("Aucun titre retrouvé sur YouTube Music")
        desc = f"Exportée depuis Navidrome par YTM Sync le {dt.date.today():%d/%m/%Y}"
        r = yt.create_playlist(name, desc, privacy_status=privacy, video_ids=ids)
        if not isinstance(r, str):
            raise RuntimeError(f"Création refusée par YouTube Music : {str(r)[:200]}")
        res.update(status="ok", found=len(ids), ytm_id=r)
        log(f"[Export] Terminé : {len(ids)}/{len(entries)} titre(s) retrouvé(s) — "
            f"https://music.youtube.com/playlist?list={r}")
    except Exception as ex:
        res.update(status="erreur", error=redact(ex))
        log(f"[Export] !! {redact(ex)}")
    finally:
        res["not_found"] = res["not_found"][:300]
        with store.lock:
            store.state["exports"] = ([res] + store.state.get("exports", []))[:20]
            store.save_state()
        EXPORT_LOCK.release()
    return res
