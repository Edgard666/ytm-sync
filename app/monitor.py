"""Suivi des telechargements SoulSync : etat par titre, scan Navidrome et resynchro a l'arrivee des fichiers."""
import collections
import threading
import time

from .clients import SoulSync, nd_for, soulsync_from
from .matching import norm, redact


# ------------------------------------------------------------------ Suivi des telechargements SoulSync
DL_ACTIVE = ("pending", "searching", "downloading")
DL_LABEL = {"queued": "En file (wishlist)", "pending": "En attente", "searching": "Recherche Soulseek",
            "downloading": "Téléchargement", "completed": "Téléchargé", "failed": "Échec",
            "gone": "Sorti de la wishlist", "unmatched": "Téléchargé, introuvable dans Navidrome"}


class Monitor:
    """Suit les titres demandes a SoulSync ; scanne Navidrome et resynchronise quand ils arrivent."""

    def __init__(self, store, runner):
        self.store, self.runner = store, runner
        self.pending_scan = set()  # playlists a resynchroniser
        self.last_check = 0
        self.busy = False
        self._force = threading.Event()
        self._last_err = None

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
                err = redact(ex)
                if err != self._last_err:   # une seule ligne tant que la meme erreur se repete
                    self.log(f"!! {err} (nouvelle tentative toutes les {self.store.cfg['monitor'].get('interval_min', 5)} min)")
                    self._last_err = err
                with self.store.lock:
                    self.store.state.setdefault("monitor", {}).update(last_error=err, last_check=time.time())
                self.last_check = time.time()
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
            requested = [e for e in self.store.state.get("tracks", {}).values()
                         if e.get("status") == "requested" and e.get("dl_status") != "unmatched"]
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
        if self._last_err:
            self.log("SoulSync de nouveau joignable")
            self._last_err = None
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
