"""Routes : actions sur les titres (lier, delier, ignorer, redemander, ameliorer, suggestions, journal des corrections)."""
import copy
import time

from flask import jsonify, request
from .clients import soulsync_from
from .core import AUDIT_LABEL, ND_EMPTY, app, login_required, store
from .matching import pl_id
from .monitor import DL_LABEL
from .services import _label, _nd, _nd_fields, audit, find_duplicates, set_ignored


@app.route("/api/navidrome/search")
@login_required
def api_nd_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify(ok=True, songs=[])
    try:
        songs = _nd().search_songs(q, 40)
        return jsonify(ok=True, songs=[{"id": x["id"], "artist": x.get("artist"), "title": x.get("title"),
                                        "album": x.get("album"), "duration": x.get("duration"),
                                        "path": x.get("path")} for x in songs])
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/tracks/<vid>/suggest")
@login_required
def api_suggest(vid):
    from .engine import get_library, suggest
    e = store.state.get("tracks", {}).get(vid)
    if not e:
        return jsonify(ok=False, msg="Titre inconnu")
    try:
        lib = get_library(_nd())
        return jsonify(ok=True, songs=suggest(lib.songs, e.get("title"), e.get("artist"), e.get("duration") or 0))
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/tracks/<vid>/link", methods=["POST"])
@login_required
def api_link(vid):
    nd_id = (request.get_json(silent=True) or {}).get("nd_id")
    try:
        song = _nd().get_song(nd_id)
    except Exception as ex:
        return jsonify(ok=False, msg=f"Morceau Navidrome introuvable : {ex}")
    with store.lock:
        before = copy.deepcopy(store.cfg.get("overrides", {}).get(vid))
        ov = store.cfg.setdefault("overrides", {}).setdefault(vid, {})
        ov["nd_id"] = nd_id
        ov["blocked"] = [b for b in ov.get("blocked", []) if b != nd_id]
        store.save_cfg()
        e = store.state["tracks"].get(vid)
        was_requested = bool(e and e.get("status") == "requested")
        if e:
            e.update(status="present", manual=True, requested_at=None, error=None, warn=None, dl_status=None,
                     **_nd_fields(song))
            store.save_state()
    if was_requested:   # deja demande a SoulSync : on le retire, sinon il serait telecharge en double
        ss = soulsync_from(store.cfg)
        if ss:
            try:
                ss.delete_wishlist(f"ytm_{vid}")
            except Exception:
                pass
    audit("link", [vid], f"{_label(vid)} lié à « {song.get('artist')} — {song.get('title')} »",
          undo={"type": "override", "vid": vid, "before": before})
    return jsonify(ok=True)


@app.route("/api/tracks/<vid>/unlink", methods=["POST"])
@login_required
def api_unlink(vid):
    with store.lock:
        e = store.state["tracks"].get(vid) or {}
        before = copy.deepcopy(store.cfg.get("overrides", {}).get(vid))
        refused = f"{e.get('nd_artist')} — {e.get('nd_title')}"
        ov = store.cfg.setdefault("overrides", {}).setdefault(vid, {})
        if e.get("nd_id") and e["nd_id"] not in ov.setdefault("blocked", []):
            ov["blocked"].append(e["nd_id"])
        ov.pop("nd_id", None)
        store.save_cfg()
        if e:
            e.update(status="missing", manual=False, blocked=len(ov["blocked"]), nd_id=None, nd_artist=None,
                     nd_title=None, nd_album=None, nd_duration=None, nd_path=None, warn=None)
            store.save_state()
    audit("unlink", [vid], f"{_label(vid)} : correspondance « {refused} » refusée",
          undo={"type": "override", "vid": vid, "before": before})
    return jsonify(ok=True)


@app.route("/api/tracks/<vid>/reset", methods=["POST"])
@login_required
def api_reset(vid):
    with store.lock:
        before = copy.deepcopy(store.cfg.get("overrides", {}).get(vid))
        store.cfg.setdefault("overrides", {}).pop(vid, None)
        store.save_cfg()
        e = store.state["tracks"].get(vid)
        if e:
            e.update(manual=False, blocked=0)
            store.save_state()
    audit("reset", [vid], f"{_label(vid)} : corrections effacées",
          undo={"type": "override", "vid": vid, "before": before})
    return jsonify(ok=True)


@app.route("/api/audit")
@login_required
def api_audit():
    rows = [dict(a, label=AUDIT_LABEL.get(a["action"], a["action"]), can_undo=bool(a.get("undo")) and not a.get("undone"))
            for a in store.state.get("audit", [])]
    for r in rows:
        r.pop("undo", None)
    return jsonify(ok=True, rows=rows)


@app.route("/api/audit/<aid>/undo", methods=["POST"])
@login_required
def api_audit_undo(aid):
    entry = next((a for a in store.state.get("audit", []) if a["id"] == aid), None)
    if not entry or not entry.get("undo") or entry.get("undone"):
        return jsonify(ok=False, msg="Rien à annuler"), 400
    u = entry["undo"]
    if u["type"] in ("ignore", "unignore"):
        set_ignored(u["vids"], u["type"] == "ignore", record=False)
    elif u["type"] == "override":
        with store.lock:
            ovs = store.cfg.setdefault("overrides", {})
            if u["before"]:
                ovs[u["vid"]] = u["before"]
            else:
                ovs.pop(u["vid"], None)
            store.save_cfg()
            e = store.state["tracks"].get(u["vid"])
            if e and e.get("status") != "ignored":   # reevalue a la prochaine synchro
                e.update(status="missing", manual=False, blocked=len((u["before"] or {}).get("blocked", [])),
                         warn=None, **ND_EMPTY)
            store.save_state()
    with store.lock:
        entry["undone"] = True
    audit("undo", entry["vids"], f"Annulé : {entry['detail']}")
    return jsonify(ok=True)


@app.route("/api/tracks/<vid>/ignore", methods=["POST"])
@login_required
def api_ignore(vid):
    on = (request.get_json(silent=True) or {}).get("ignore", True)
    return jsonify(ok=True, removed=set_ignored([vid], on))


@app.route("/api/tracks/bulk", methods=["POST"])
@login_required
def api_bulk():
    j = request.get_json(silent=True) or {}
    ids, act = [str(x) for x in (j.get("ids") or [])][:5000], j.get("action")
    if act in ("ignore", "unignore"):
        return jsonify(ok=True, removed=set_ignored(ids, act == "ignore"), count=len(ids))
    if act == "rerequest":
        return api_rerequest_ids(ids)
    if act == "upgrade":
        return api_upgrade(ids)
    return jsonify(ok=False, msg="action inconnue"), 400


def api_upgrade(ids):
    """Redemande a SoulSync des titres presents mais en qualite faible."""
    ss = soulsync_from(store.cfg)
    if not ss:
        return jsonify(ok=False, msg="SoulSync non configuré")
    sent, errs = 0, []
    for vid in ids:
        e = store.state["tracks"].get(vid)
        if not e or e.get("status") != "present":
            continue
        code, txt = ss.add({"videoId": vid, "title": e.get("title"), "artists": [e.get("artist") or "Unknown"],
                            "album": e.get("album"), "duration_ms": (e.get("duration") or 0) * 1000})
        if code < 300:
            sent += 1
            with store.lock:
                e["upgrade_at"] = time.time()
        else:
            errs.append(f"{_label(vid)} : HTTP {code}")
    if sent and store.cfg["sync"]["trigger_process"]:
        ss.process()
    with store.lock:
        store.save_state()
    audit("rerequest", ids, f"Amélioration de qualité demandée : {sent} titre(s)")
    return jsonify(ok=not errs, sent=sent, msg="; ".join(errs[:5]))


@app.route("/api/tracks")
@login_required
def api_tracks():
    names = {p["id"]: (store.state["playlists"].get(p["id"], {}).get("title") or pl_id(p["source"]))
             for p in store.cfg["playlists"]}
    keys = ("videoId", "artist", "title", "album", "duration", "nd_artist", "nd_title", "nd_album", "nd_duration",
            "nd_path", "status", "dl_status", "dl_progress", "dl_error", "warn", "manual", "blocked",
            "ignore_reason", "requested_at", "error", "low_quality", "nd_quality", "upgrade_at")
    with store.lock:
        rows = [dict({k: e.get(k) for k in keys}, playlists=", ".join(names.get(p, "?") for p in e.get("playlists", [])))
                for e in store.state.get("tracks", {}).values()]
    return jsonify(ok=True, rows=rows, dl_label=DL_LABEL)


@app.route("/api/tracks/rerequest", methods=["POST"])
@login_required
def api_rerequest():
    return api_rerequest_ids((request.get_json(silent=True) or {}).get("ids"))


def api_rerequest_ids(ids):
    if ids:
        audit("rerequest", ids, ", ".join(_label(v) for v in ids[:3]) + (f" et {len(ids) - 3} autre(s)" if len(ids) > 3 else ""))
    else:
        audit("rerequest", [], "Tous les titres non présents")
    with store.lock:
        for vid, e in store.state["tracks"].items():
            if (ids is None or vid in ids) and e.get("status") not in ("present", "ignored"):
                e["requested_at"] = None
                e["dl_status"] = e["dl_error"] = None
                if e.get("status") == "requested":
                    e["status"] = "missing"
        store.save_state()
    return jsonify(ok=True)


@app.route("/api/cleanup/duplicates")
@login_required
def api_duplicates():
    try:
        groups, n = find_duplicates()
        return jsonify(ok=True, groups=groups, library=n)
    except Exception as ex:
        return jsonify(ok=False, msg=str(ex))


@app.route("/api/cleanup/forget", methods=["POST"])
@login_required
def api_cleanup_forget():
    """Retire des titres de la liste de menage (le fichier n'est jamais touche)."""
    ids = (request.get_json(silent=True) or {}).get("ids") or []
    with store.lock:
        for vid in ids:
            store.state.get("downloaded", {}).pop(vid, None)
        store.save_state()
    return jsonify(ok=True)
