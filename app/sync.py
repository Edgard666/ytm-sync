"""Synchronisation : YouTube Music -> Navidrome (playlists miroirs) -> SoulSync, et planificateur."""
import collections
import copy
import datetime as dt
import os
import re
import secrets
import threading
import time

from .clients import _LIB_CACHE, get_library, nd_for, soulsync_from, watch_as_playlist, ytm_account, ytm_client
from .matching import low_quality, match_rule, parse_rules, pl_id, redact, song_key
from .store import AUTH_F, LOG_DIR, VERSION


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
        line = f"{dt.datetime.now():%H:%M:%S}  {redact(msg)}"
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
        summary = {"start": time.time(), "trigger": trigger, "status": "ok", "message": "", "log": ""}
        details = {}
        try:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                fn = os.path.join(LOG_DIR, f"run_{dt.datetime.now():%Y%m%d_%H%M%S}.log")
                self._logf = open(fn, "w", encoding="utf-8")
                summary["log"] = os.path.basename(fn)
            except OSError as ex:   # disque plein, droits... : la synchro tourne quand meme, sans fichier
                self._logf = None
                self.log(f"!! Journal impossible à créer : {ex}")
            res = self._run(trigger, dry, only)
            details = res.pop("_details", {})
            summary.update(res)
            if self.cancel:
                summary["status"] = "annulé"
        except Exception as ex:
            summary.update(status="erreur", message=str(ex))
            self.log(f"!! ERREUR : {ex}")
        finally:
            # chaque etape de nettoyage est isolee : la synchro ne reste jamais « en cours »
            summary["end"] = time.time()
            try:
                with self.store.lock:
                    self.store.state["last_run"] = summary
                    self.store.state["history"] = ([summary] + self.store.state["history"])[:30]
                    self.store.save_state()
            except Exception as ex:
                self.log(f"!! Enregistrement de l'état impossible : {ex}")
            try:
                self._notify(summary, details)
            except Exception as ex:
                self.log(f"!! Notification : {ex}")
            self.log(f"=== Fin ({summary['status']}) ===")
            try:
                if self._logf:
                    self._logf.close()
            except OSError:
                pass
            self._logf = None
            try:
                self._prune_logs()
            except Exception:
                pass
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
        if sm.get("unmatched"):
            lines.append(f"{sm['unmatched']} titre(s) téléchargé(s) mais introuvable(s) dans Navidrome : à lier à la main")
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
        with self.store.lock:
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
                lib = get_library(nd, self.log)
                if lib.songs:
                    searcher = lib
                else:
                    self.log("Bibliothèque vide via le cache : recherche par l'API")
            except Exception as ex:
                _LIB_CACHE.clear()
                self.log(f"?? Cache bibliothèque indisponible ({ex}) : recherche par l'API")
        ss = soulsync_from(cfg)
        if not ss:
            self.log("SoulSync non configuré : les manquants seront seulement listés")
        yt = ytm_client()
        self.log("YouTube Music : " + ("mode connecté" if os.path.exists(AUTH_F) else "mode anonyme (playlists publiques)"))
        if os.path.exists(AUTH_F):
            self._check_ytm_session(cfg)

        old = self.store.state.get("tracks", {})
        tracks = {} if only is None else copy.deepcopy(old)
        downloaded = {}
        st = dict(total=0, present=0, missing=0, sent=0, pending=0, errors=0, warns=0, ignored=0, pl_changed=0,
                  unmatched=0, low_quality=0)
        self._min_kbps = int(s.get("min_bitrate") or 0)
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

        def keep_old(p_id):
            """Playlist non lue : on garde ses titres tels quels (pas de redemande, pas de faux orphelins)."""
            for v, o in old.items():
                if p_id in (o.get("playlists") or []):
                    if v not in tracks:
                        tracks[v] = copy.deepcopy(o)
                        tracks[v].pop("_run", None)
                    elif p_id not in tracks[v]["playlists"]:
                        tracks[v]["playlists"].append(p_id)

        if only is None and cfg.get("auto_watch", {}).get("enabled") and os.path.exists(AUTH_F):
            added = self._auto_watch(cfg, yt)
            if added:
                cfg = copy.deepcopy(self.store.cfg)   # inclut les playlists qui viennent d'etre ajoutees
        used_targets = {}
        pls = [p for p in cfg["playlists"] if p.get("enabled", True) and (only is None or p["id"] in only)]
        fetched = self._fetch_playlists([pl_id(p["source"]) for p in pls])
        for pi, p in enumerate(pls, 1):
            if self.cancel:
                break
            pid = pl_id(p["source"])
            prev_pl = self.store.state["playlists"].get(p["id"], {})
            try:
                pl = fetched[pid]
                if isinstance(pl, Exception):
                    raise pl
            except Exception as ex:
                self.log(f"!! Playlist {pid} illisible : {ex} — titres précédents conservés")
                st["errors"] += 1
                keep_old(p["id"])
                self._pl_state(p["id"], {"error": f"Illisible : {ex}", "last_sync": now})
                continue
            items = pl.get("tracks", []) or []
            if not items and prev_pl.get("total"):
                msg = (f"vue vide côté YouTube (avant : {prev_pl['total']} titres) — playlist privée ou session "
                       f"expirée ? Playlist Navidrome conservée telle quelle")
                self.log(f"!! {pl.get('title') or pid} : {msg}")
                st["errors"] += 1
                keep_old(p["id"])
                self._pl_state(p["id"], {"error": msg, "last_sync": now})
                continue
            nd_name = p.get("nd_name") or npl["prefix"] + (pl.get("title") or pid)
            acc = p.get("nd_account", "")
            self.log(f"### {pl.get('title')} ({len(items)} titres) → {nd_name}")
            song_ids, pst = [], dict(total=0, present=0, missing=0, ignored=0)
            labels, diff = {}, None
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
                        labels[e["nd_id"]] = f"{e.get('nd_artist')} — {e.get('nd_title')}"
                else:
                    pst["missing"] += 1
            result, nd_pl_id = "désactivé", prev_pl.get("nd_pl_id")
            target = (acc, nd_name.strip().lower())
            if npl["enabled"] and not self.cancel and target in used_targets:
                result = f"ERREUR : nom déjà utilisé par la playlist « {used_targets[target]} » — donne-lui un autre nom"
                st["errors"] += 1
                self.log(f"  !! {result}")
            elif npl["enabled"] and not self.cancel:
                used_targets[target] = pl.get("title") or pid
                try:
                    if acc not in nd_clients:
                        nd_clients[acc] = nd_for(cfg, acc)
                        nd_lists[acc] = nd_clients[acc].owned_playlists()
                    known = prev_pl.get("nd_pl_id") if prev_pl.get("nd_account", "") == acc else None
                    result, changed, nd_pl_id = nd_clients[acc].sync_playlist(
                        nd_name, song_ids, nd_lists[acc], npl["public"], DRY, known_id=known,
                        description=pl.get("description") if npl.get("copy_description", True) else None,
                        labels=labels)
                    diff = getattr(nd_clients[acc], "last_diff", None)
                    st["pl_changed"] += int(changed)
                    if acc:
                        result += f" (compte {nd_clients[acc].user})"
                except Exception as ex:
                    result = f"ERREUR {ex}"
                    st["errors"] += 1
                self.log(f"  >> Playlist Navidrome « {nd_name} » : {result}")
            self._pl_state(p["id"], dict(title=pl.get("title"), nd_name=nd_name, nd_result=result,
                                         nd_pl_id=nd_pl_id, nd_account=acc, last_sync=now, error=None,
                                         diff=dict(diff, dry=DRY, at=now) if diff else None, **pst))

        if s.get("star_likes") and os.path.exists(AUTH_F) and not self.cancel:
            try:
                self._sync_stars(cfg, nd, searcher, fetched, tracks, DRY, t_th, a_th, strict_versions)
            except Exception as ex:
                self.log(f"!! Favoris Navidrome : {ex}")
                st["errors"] += 1

        if self.cancel:
            # arret demande : les playlists non (ou partiellement) traitees gardent leurs titres,
            # sinon ils seraient redemandes a SoulSync au passage suivant
            for p in pls:
                keep_old(p["id"])
            self.log("Synchro arrêtée : les titres non traités sont conservés tels quels")

        for e in tracks.values():
            e.pop("_run", None)
        with self.store.lock:
            self._reconcile(cfg, tracks, ss, DRY, nd)
            self.store.state["tracks"] = tracks
            if not DRY:
                self.store.state.setdefault("downloaded", {}).update(downloaded)
            if not DRY and only is None and not self.cancel:
                c = collections.Counter(e.get("status") for e in tracks.values())
                day = dt.date.today().isoformat()
                cov = [x for x in self.store.state.get("coverage", []) if x["d"] != day]
                cov.append({"d": day, "present": c["present"], "total": len(tracks) - c["ignored"]})
                self.store.state["coverage"] = cov[-365:]
            self.store.save_state()

        if ss and st["sent"] and not DRY and s["trigger_process"]:
            code = ss.process()
            self.log(f"Traitement wishlist SoulSync : HTTP {code}" + (" (déjà en cours)" if code == 409 else ""))
        if downloaded:
            self.log(f"{len(downloaded)} titre(s) demandé(s) précédemment sont maintenant dans Navidrome")
        self.log(f"Résumé : {st['total']} titres | {st['present']} présents | {st['missing']} manquants | "
                 f"{st['sent']} envoyés | {st['pending']} en attente | {st['ignored']} ignorés | "
                 f"{st['warns']} durées à vérifier | {st['unmatched']} à lier | {st['low_quality']} en qualité faible | "
                 f"{st['errors']} erreurs")
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
                     nd_quality=(f"{(hit.get('suffix') or '?').upper()}" + (f" {hit['bitRate']} kbps" if hit.get("bitRate") else ""))
                     if hit.get("suffix") or hit.get("bitRate") else None,
                     low_quality=low_quality(hit, self._min_kbps),
                     dl_status=None, dl_progress=None, dl_error=None)
            if e["low_quality"]:
                st["low_quality"] += 1
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
        if e.get("requested_at") and e.get("dl_status") in ("completed", "gone", "unmatched"):
            # SoulSync l'a telecharge mais on ne le retrouve pas : surtout ne pas le retelecharger
            if e["dl_status"] != "unmatched":
                self.log(f"  ?! téléchargé mais introuvable dans Navidrome (nom différent ?) : {label} "
                         f"— utilise « Lier… » dans la page Titres")
            e.update(status="requested", dl_status="unmatched")
            st["unmatched"] += 1
            return
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

    def _reconcile(self, cfg_start, tracks, ss, DRY, nd):
        """Applique les corrections faites dans l'interface PENDANT la synchro (ignorer, lier, delier…),
        que la synchro ne pouvait pas voir (elle travaille sur une copie de la configuration)."""
        now_cfg = self.store.cfg
        ign_start, ign_now = set(cfg_start.get("ignored", {})), set(now_cfg.get("ignored", {}))
        keys_now = {v.get("key") for v in now_cfg.get("ignored", {}).values() if v.get("key")}
        keys_new = keys_now - {v.get("key") for v in cfg_start.get("ignored", {}).values() if v.get("key")}
        ov_start, ov_now = cfg_start.get("overrides", {}), now_cfg.get("overrides", {})
        changed = 0
        for vid, e in tracks.items():
            newly_ignored = e.get("status") != "ignored" and (vid in ign_now - ign_start or e.get("key") in keys_new)
            if newly_ignored:
                if e.get("status") == "requested" and ss and not DRY:
                    try:
                        ss.delete_wishlist(f"ytm_{vid}")
                    except Exception:
                        pass
                e.update(status="ignored", requested_at=None, dl_status=None, warn=None,
                         ignore_reason="ignoré manuellement" if vid in ign_now else "même morceau qu'un titre ignoré",
                         nd_id=None, nd_artist=None, nd_title=None, nd_album=None, nd_duration=None, nd_path=None)
                changed += 1
            elif vid in ign_start - ign_now and e.get("status") == "ignored":
                e.update(status="missing", ignore_reason=None)
                changed += 1
            elif ov_start.get(vid) != ov_now.get(vid) and e.get("status") != "ignored":
                ov = ov_now.get(vid) or {}
                song = None
                if ov.get("nd_id"):
                    try:
                        song = nd.get_song(ov["nd_id"])
                    except Exception:
                        song = None
                if song:
                    if e.get("status") == "requested" and ss and not DRY:
                        try:
                            ss.delete_wishlist(f"ytm_{vid}")
                        except Exception:
                            pass
                    e.update(status="present", manual=True, requested_at=None, dl_status=None, nd_id=song["id"],
                             nd_artist=song.get("artist"), nd_title=song.get("title"), nd_album=song.get("album"),
                             nd_duration=song.get("duration"), nd_path=song.get("path"))
                else:   # delie / reinitialise : reevalue a la prochaine synchro
                    e.update(status="missing", manual=False, nd_id=None, nd_artist=None, nd_title=None,
                             nd_album=None, nd_duration=None, nd_path=None)
                e["blocked"] = len(ov.get("blocked", []))
                changed += 1
        if changed:
            self.log(f"{changed} correction(s) faite(s) pendant la synchro prise(s) en compte")

    def _auto_watch(self, cfg, yt):
        """Ajoute a la surveillance les playlists de la bibliotheque YouTube (toutes, ou par prefixe)."""
        aw = cfg["auto_watch"]
        try:
            lib = yt.get_library_playlists(limit=None) or []
        except Exception as ex:
            self.log(f"?? Surveillance automatique : bibliothèque YouTube illisible ({ex})")
            return 0
        have = {pl_id(p["source"]) for p in cfg["playlists"]}
        dismissed = set(aw.get("dismissed", []))
        prefix = (aw.get("prefix") or "").strip().lower()
        new = []
        for p in lib:
            pid, title = p.get("playlistId"), (p.get("title") or "")
            if not pid or pid in ("LM", "SE") or pid in have or pid in dismissed:
                continue
            if aw.get("mode") == "prefix" and not title.lower().startswith(prefix):
                continue
            new.append({"id": secrets.token_hex(4), "source": pid, "nd_name": "", "enabled": True,
                        "nd_account": aw.get("nd_account", ""), "auto": True, "title_hint": title})
        if new:
            with self.store.lock:
                self.store.cfg["playlists"].extend(new)
                self.store.save_cfg()
                self.store.state.setdefault("audit", []).insert(0, {
                    "id": secrets.token_hex(5), "at": time.time(), "action": "playlist", "vids": [],
                    "detail": "Surveillance automatique : " + ", ".join(f"« {x['title_hint']} »" for x in new),
                    "undo": None, "undone": False})
            self.log(f"Surveillance automatique : {len(new)} nouvelle(s) playlist(s) ajoutée(s) — "
                     + ", ".join(x["title_hint"] for x in new))
        return len(new)

    def _sync_stars(self, cfg, nd, searcher, fetched, tracks, DRY, t_th, a_th, strict_versions):
        """« J'aime » YouTube Music -> favoris (etoile) Navidrome."""
        liked = fetched.get("LM")
        if liked is None:
            liked = self._fetch_playlists(["LM"]).get("LM")
        if isinstance(liked, Exception):
            raise liked
        overrides = cfg.get("overrides", {})
        want = []
        for tr in liked.get("tracks", []) or []:
            vid = tr.get("videoId")
            if vid in cfg.get("ignored", {}):
                continue
            e = tracks.get(vid)
            if e and e.get("status") == "present":
                want.append(e["nd_id"])
                continue
            ov = overrides.get(vid, {})
            if ov.get("nd_id"):
                want.append(ov["nd_id"])
                continue
            artists = [a["name"] for a in (tr.get("artists") or []) if a.get("name")]
            hit = searcher.find(tr.get("title") or "", artists[0] if artists else "", t_th, a_th,
                                album=(tr.get("album") or {}).get("name"), duration=tr.get("duration_seconds") or 0,
                                strict_versions=strict_versions, blocked=set(ov.get("blocked", [])))
            if hit:
                want.append(hit["id"])
        want = list(dict.fromkeys(want))
        liked_n = len(liked.get("tracks", []) or [])
        prev_n = self.store.state.get("liked_count", 0)
        safe_to_unstar = liked_n > 0 and not (prev_n >= 10 and liked_n < prev_n * 0.5)
        if not safe_to_unstar and cfg["sync"].get("unstar_removed") and self.store.state.get("starred_by_app"):
            self.log(f"!! « J'aime » YouTube : {liked_n} titre(s) au lieu de {prev_n} — session expirée ? "
                     f"Aucun favori retiré par sécurité")
        starred = {x["id"] for x in nd.call("getStarred2").get("starred2", {}).get("song", [])}
        mine = set(self.store.state.get("starred_by_app", []))
        to_star = [i for i in want if i not in starred]
        to_unstar = [i for i in mine if i not in want and i in starred] \
            if cfg["sync"].get("unstar_removed") and safe_to_unstar else []
        verb = "seraient" if DRY else "ont été"
        if to_star:
            if not DRY:
                for i in range(0, len(to_star), 100):
                    nd.call("star", _data=[("id", x) for x in to_star[i:i + 100]])
            self.log(f"★ {len(to_star)} titre(s) aimé(s) sur YouTube {verb} ajouté(s) aux favoris Navidrome")
        if to_unstar:
            if not DRY:
                for i in range(0, len(to_unstar), 100):
                    nd.call("unstar", _data=[("id", x) for x in to_unstar[i:i + 100]])
            self.log(f"☆ {len(to_unstar)} favori(s) {verb} retiré(s) (plus aimés sur YouTube)")
        if not DRY:
            with self.store.lock:
                self.store.state["starred_by_app"] = sorted((mine | set(to_star)) - set(to_unstar))
                if liked_n:
                    self.store.state["liked_count"] = liked_n
        if not to_star and not to_unstar:
            self.log(f"★ Favoris Navidrome à jour ({len(want)} titre(s) aimé(s) retrouvé(s))")

    def _fetch_playlists(self, pids, workers=4):
        """Lit les playlists YouTube en parallele (1 a 3 s chacune). Resultat : {id: playlist | Exception}."""
        from concurrent.futures import ThreadPoolExecutor

        def one(pid):
            try:
                yt = ytm_client()  # un client par fil : la session HTTP n'est pas partagee
                if pid == "LM":    # « Titres que j'aime » (compte connecte requis)
                    pl = yt.get_liked_songs(limit=None)
                    pl["title"] = pl.get("title") or "Titres que j'aime"
                    return pl
                try:
                    return yt.get_playlist(pid, limit=None)
                except Exception:
                    if not pid.startswith("RD"):
                        raise
                    return watch_as_playlist(yt, pid)   # mixes personnalises (Supermix, Decouvertes…)
            except Exception as ex:
                return ex
        uniq = list(dict.fromkeys(pids))
        if not uniq:
            return {}
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=min(workers, len(uniq))) as ex:
            res = dict(zip(uniq, ex.map(one, uniq)))
        self.log(f"{len(uniq)} playlist(s) YouTube lue(s) en {time.time() - t0:.1f} s")
        return res

    def _check_ytm_session(self, cfg):
        """Alerte (une seule fois) quand la session YouTube Music expire."""
        try:
            ytm_account()
            if self.store.state.get("ytm_auth_error"):
                self.log("Session YouTube Music de nouveau valide")
            with self.store.lock:
                self.store.state["ytm_auth_error"] = None
        except Exception as ex:
            err = str(ex)[:200]
            self.log(f"!! Session YouTube Music expirée ou invalide : {err}")
            if not self.store.state.get("ytm_auth_error"):
                from . import notify
                notify.send(cfg, "YTM Sync — session YouTube expirée",
                            "Les playlists privées ne peuvent plus être lues. Reconnecte ton compte dans "
                            "« Compte YouTube » (recopie des en-têtes du navigateur).", "high", log=self.log)
            with self.store.lock:
                self.store.state["ytm_auth_error"] = err

    def _pl_state(self, pid, data):
        with self.store.lock:
            cur = self.store.state["playlists"].get(pid, {})
            cur.update(data)
            self.store.state["playlists"][pid] = cur


# ------------------------------------------------------------------ Planificateur
CATCHUP_DELAY = 120  # rattrapage d'une synchro manquee : 2 min apres le demarrage (le temps que tout demarre)


HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
HEARTBEAT = {}   # derniere boucle de chaque tache de fond (pour /healthz)


def parse_hhmm(v):
    """'03:00' -> (3, 0) ; None si l'heure est invalide (25:00, 3h, vide...)."""
    m = HHMM_RE.match(str(v or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def next_run_ts(sch, last_start, app_start):
    """Prochaine synchro. Calculee depuis la DERNIERE synchro (et non depuis le demarrage) : si le conteneur
    etait arrete a l'heure prevue, la synchro manquee est rattrapee peu apres le redemarrage."""
    if not sch.get("enabled"):
        return None
    base = last_start or app_start
    if sch.get("mode") == "daily":
        hh, mm = parse_hhmm(sch.get("daily_time")) or (3, 0)
        b = dt.datetime.fromtimestamp(base)
        cand = b.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() <= base:
            cand += dt.timedelta(days=1)
        nxt = cand.timestamp()
    else:
        nxt = base + max(0.25, float(sch.get("interval_hours", 6))) * 3600
    if last_start and nxt < app_start + CATCHUP_DELAY:   # manquee pendant l'arret
        nxt = app_start + CATCHUP_DELAY
    return nxt


def scheduler_loop(store, runner):
    while True:
        HEARTBEAT["scheduler"] = time.time()
        try:
            if store.daily_backup():
                runner.log("Sauvegarde quotidienne de la configuration effectuée (dossier backups)")
        except Exception as ex:
            runner.log(f"!! Sauvegarde quotidienne : {ex}")
        try:
            lr = store.state.get("last_run") or {}
            nxt = next_run_ts(store.cfg["schedule"], lr.get("start"), runner.started_at)
            if nxt and time.time() >= nxt and not runner.running:
                runner.start(trigger="planifié")
        except Exception as ex:  # ne jamais tuer le planificateur
            runner.log(f"!! Planificateur : {ex}")
        time.sleep(20)
