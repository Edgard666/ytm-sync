"""Routes : exports CSV (titres, doublons, menage)."""
import datetime as dt

from flask import Response, request
from .core import app, dtf, login_required, store
from .matching import pl_id
from .monitor import DL_LABEL
from .services import csv_row, find_duplicates, orphans


@app.route("/export/tracks.csv")
@login_required
def export_tracks():
    """CSV (Excel FR : separateur ';', UTF-8 avec BOM) pour verifier les correspondances."""
    import csv
    import io
    from flask import Response
    want = request.args.get("status", "all")
    names = {p["id"]: (store.state["playlists"].get(p["id"], {}).get("title") or pl_id(p["source"]))
             for p in store.cfg["playlists"]}
    lab = {"present": "Présent", "missing": "Manquant", "requested": "Demandé", "error": "Erreur",
           "ignored": "Ignoré"}

    def mmss(sec):
        return f"{int(sec) // 60}:{int(sec) % 60:02d}" if sec else ""

    rows = [e for e in store.state.get("tracks", {}).values()
            if want == "all" or e.get("status") == want
            or (want == "missing" and e.get("status") in ("missing", "requested", "error"))]
    rows.sort(key=lambda e: (e.get("status") != "missing", (e.get("artist") or "").lower(), e.get("title") or ""))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Statut", "Artiste YouTube", "Titre YouTube", "Album YouTube", "Durée YouTube",
                "Artiste Navidrome", "Titre Navidrome", "Album Navidrome", "Durée Navidrome",
                "Fichier Navidrome", "Correction manuelle", "Avertissement", "Playlists", "Demandé à SoulSync le",
                "Téléchargement SoulSync", "Raison ignoré", "Erreur", "Lien YouTube"])
    for e in rows:
        csv_row(w, [lab.get(e.get("status"), e.get("status")), e.get("artist"), e.get("title"),
                    e.get("album") or "", mmss(e.get("duration")),
                    e.get("nd_artist") or "", e.get("nd_title") or "", e.get("nd_album") or "",
                    mmss(e.get("nd_duration")), e.get("nd_path") or "",
                    ("Lien manuel" if e.get("manual") else "") +
                    (f"{' + ' if e.get('manual') else ''}{e['blocked']} lien(s) refusé(s)" if e.get("blocked") else ""),
                    e.get("warn") or "",
                    ", ".join(names.get(p, "?") for p in e.get("playlists", [])),
                    dtf(e.get("requested_at")) if e.get("requested_at") else "",
                    (DL_LABEL.get(e.get("dl_status"), "") + (f" — {e['dl_error']}" if e.get("dl_error") else ""))
                    if e.get("status") == "requested" else "",
                    e.get("ignore_reason") or "" if e.get("status") == "ignored" else "",
                    e.get("error") or "", f"https://music.youtube.com/watch?v={e.get('videoId')}"])
    fname = f"ytm-sync_{want}_{dt.datetime.now():%Y%m%d_%H%M}.csv"
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.route("/export/duplicates.csv")
@login_required
def export_duplicates():
    import csv
    import io
    groups, _ = find_duplicates()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Artiste", "Titre", "Album", "Qualité", "Fichier", "Meilleure version"])
    for g in groups:
        for f in g["files"]:
            csv_row(w, [g["artist"], g["title"], f["album"], f["quality"], f["path"], "oui" if f["best"] else ""])
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=ytm-sync_doublons_{dt.datetime.now():%Y%m%d_%H%M}.csv"})


@app.route("/export/cleanup.csv")
@login_required
def export_cleanup():
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Artiste", "Titre", "Album Navidrome", "Fichier Navidrome", "Arrivé le", "Raison", "Lien YouTube"])
    for o in orphans():
        csv_row(w, [o.get("nd_artist") or o.get("artist"), o.get("nd_title") or o.get("title"),
                    o.get("nd_album") or "", o.get("nd_path") or "", dtf(o.get("at")), o["reason"],
                    f"https://music.youtube.com/watch?v={o['videoId']}"])
    return Response("\ufeff" + buf.getvalue(), content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=ytm-sync_menage_{dt.datetime.now():%Y%m%d_%H%M}.csv"})
