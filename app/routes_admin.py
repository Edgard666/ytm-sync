"""Routes : parametres, sauvegardes, restauration, cle API."""
import copy
import datetime as dt
import json
import os
import re
import secrets
import time

from flask import Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash
from .core import (
    BACKUP_RE,
    CLEAR_MARK,
    LAST_PEER,
    MIN_PW,
    SECRET_MASK,
    SECRET_PATHS,
    _key_hash,
    app,
    login_required,
    new_epoch,
    store)
from .services import audit, list_backups
from .store import VERSION


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    c = store.cfg
    if request.method == "POST":
        f = request.form
        snapshot = copy.deepcopy(c)

        def keep(new, old):
            if new == CLEAR_MARK:      # bouton « Effacer » a cote d'un champ secret
                return ""
            return old if new in ("", SECRET_MASK) else new

        c["navidrome"].update(url=f.get("nd_url", "").strip(), user=f.get("nd_user", "").strip(),
                              password=keep(f.get("nd_pass", ""), c["navidrome"]["password"]))
        c["soulsync"].update(url=f.get("ss_url", "").strip(), profile_id=f.get("ss_profile", "1").strip() or "1",
                             api_key=keep(f.get("ss_key", "").strip(), c["soulsync"]["api_key"]))
        try:
            c["sync"].update(dry_run=f.get("dry_run") == "on", trigger_process=f.get("trigger_process") == "on",
                             scan_before=f.get("scan_before") == "on",
                             title_threshold=min(1.0, max(0.3, float(f.get("title_threshold", 0.85)))),
                             artist_threshold=min(1.0, max(0.0, float(f.get("artist_threshold", 0.6)))),
                             re_request_days=max(0, int(f.get("re_request_days", 7))),
                             duration_tolerance=max(0, int(f.get("duration_tolerance", 15))),
                             strict_versions=f.get("strict_versions") == "on",
                             library_cache=f.get("library_cache") == "on",
                             star_likes=f.get("star_likes") == "on", unstar_removed=f.get("unstar_removed") == "on",
                             min_bitrate=max(0, int(f.get("min_bitrate") or 0)),
                             exclude_rules=f.get("exclude_rules", "").strip())
            c["monitor"].update(enabled=f.get("mon_enabled") == "on", auto_scan=f.get("mon_scan") == "on",
                                resync_after=f.get("mon_resync") == "on",
                                interval_min=max(1, int(f.get("mon_interval", 5))))
            n = c["notify"]
            n.update(on_sync=f.get("nt_on_sync", "changes"), on_dl_fail=f.get("nt_dl_fail") == "on")
            n["ntfy"].update(enabled=f.get("ntfy_on") == "on", url=f.get("ntfy_url", "").strip() or "https://ntfy.sh",
                             topic=f.get("ntfy_topic", "").strip(),
                             token=keep(f.get("ntfy_token", "").strip(), n["ntfy"]["token"]))
            n["webhook"].update(enabled=f.get("wh_on") == "on", format=f.get("wh_format", "discord"),
                                url=keep(f.get("wh_url", "").strip(), n["webhook"]["url"]))
            n["gotify"].update(enabled=f.get("gotify_on") == "on", url=f.get("gotify_url", "").strip(),
                               token=keep(f.get("gotify_token", "").strip(), n["gotify"]["token"]))
            n["email"].update(enabled=f.get("mail_on") == "on", host=f.get("mail_host", "").strip(),
                              port=int(f.get("mail_port") or 587), security=f.get("mail_sec", "starttls"),
                              user=f.get("mail_user", "").strip(), **{"from": f.get("mail_from", "").strip()},
                              to=f.get("mail_to", "").strip(),
                              password=keep(f.get("mail_pass", ""), n["email"]["password"]))
            c["security"]["behind_proxy"] = f.get("behind_proxy") == "on"
            c["security"]["proxy_ips"] = f.get("proxy_ips", "").strip()
            c["security"]["idle_days"] = max(0, min(90, int(f.get("idle_days") or 0)))
            c["auto_watch"].update(enabled=f.get("aw_on") == "on", mode=f.get("aw_mode", "all"),
                                   prefix=f.get("aw_prefix", "").strip(), nd_account=f.get("aw_account", ""))
            c["mqtt"].update(enabled=f.get("mqtt_on") == "on", host=f.get("mqtt_host", "").strip(),
                             port=int(f.get("mqtt_port") or 1883), user=f.get("mqtt_user", "").strip(),
                             tls=f.get("mqtt_tls") == "on",
                             discovery_prefix=f.get("mqtt_prefix", "").strip() or "homeassistant",
                             base_topic=f.get("mqtt_base", "").strip() or "ytm_sync",
                             password=keep(f.get("mqtt_pass", ""), c["mqtt"]["password"]))
            c["nd_playlists"].update(enabled=f.get("ndpl_enabled") == "on", public=f.get("ndpl_public") == "on",
                                     prefix=f.get("ndpl_prefix", ""), copy_description=f.get("ndpl_desc") == "on")
            c["schedule"].update(enabled=f.get("sch_enabled") == "on", mode=f.get("sch_mode", "interval"),
                                 interval_hours=max(0.25, float(f.get("sch_interval", 6))),
                                 daily_time=f.get("sch_daily", "03:00") or "03:00")
            c["logs_keep"] = max(1, int(f.get("logs_keep", 30)))
        except (ValueError, TypeError):
            c.clear()                  # rien n'est applique si une valeur est invalide
            c.update(snapshot)
            flash("Valeur numérique invalide : aucun paramètre n'a été modifié.", "err")
            return redirect(url_for("settings"))
        newpw = f.get("new_password", "")
        if newpw:
            if len(newpw) < MIN_PW:
                flash(f"Nouveau mot de passe trop court ({MIN_PW} caractères minimum) : non modifié.", "err")
            else:
                c["auth"]["password_hash"] = generate_password_hash(newpw)
                new_epoch()            # deconnecte toutes les autres sessions
                session["ep"] = c["auth"]["epoch"]
                audit("password", [], "Mot de passe de l'interface modifié (autres sessions déconnectées)")
        store.save_cfg()
        flash("Paramètres enregistrés.", "ok")
        return redirect(url_for("settings"))
    return render_template("settings.html", page="set", c=c, mask=SECRET_MASK, last_peer=LAST_PEER["addr"])


@app.route("/api/settings/rules-preview", methods=["POST"])
@login_required
def api_rules_preview():
    """Titres qui seraient ignores par ces regles (d'apres la derniere synchro)."""
    from .engine import match_rule, parse_rules
    rules = parse_rules((request.get_json(silent=True) or {}).get("rules", ""))
    hits = []
    for e in store.state.get("tracks", {}).values():
        r = match_rule(rules, e.get("title"), e.get("artist"), e.get("album"))
        if r:
            hits.append(f"{e.get('artist')} - {e.get('title')}  ←  {r}")
    return jsonify(ok=True, count=len(hits), hits=sorted(hits)[:100])


@app.route("/api/backups")
@login_required
def api_backups():
    return jsonify(ok=True, backups=list_backups())


@app.route("/backups/<name>")
@login_required
def backup_file(name):
    from flask import send_from_directory
    from .store import BACKUP_DIR
    if not BACKUP_RE.match(name):
        abort(404)
    return send_from_directory(BACKUP_DIR, name, as_attachment=True)


@app.route("/api/backups/<day>/restore", methods=["POST"])
@login_required
def api_backup_restore(day):
    """Remet la configuration (et, si demande, l'etat) d'une sauvegarde quotidienne. Le mot de passe reste."""
    from .store import BACKUP_DIR, DEFAULT_CFG, DEFAULT_STATE, _merge, validate_cfg
    if not re.match(r"^\d{8}$", day):
        abort(404)
    j = request.get_json(silent=True) or {}
    try:
        with open(os.path.join(BACKUP_DIR, f"config-{day}.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        assert isinstance(cfg, dict)
    except Exception as ex:
        return jsonify(ok=False, msg=f"Sauvegarde illisible : {ex}")
    with store.lock:
        cur_auth = store.cfg["auth"]
        merged = _merge(cfg, DEFAULT_CFG)
        fixed = validate_cfg(merged)
        merged["auth"] = cur_auth
        store.cfg.clear()
        store.cfg.update(merged)
        store.save_cfg()
        if j.get("with_state"):
            try:
                with open(os.path.join(BACKUP_DIR, f"state-{day}.json"), encoding="utf-8") as f:
                    st = json.load(f)
                store.state.clear()
                store.state.update(_merge(st, DEFAULT_STATE))
                store.save_state()
            except Exception as ex:
                return jsonify(ok=False, msg=f"Configuration restaurée, mais état illisible : {ex}")
    audit("playlist", [], f"Sauvegarde du {day[6:]}/{day[4:6]}/{day[:4]} restaurée"
          + (" (avec l'état des titres)" if j.get("with_state") else ""))
    return jsonify(ok=True, fixed=fixed)


@app.route("/backup.json")
@login_required
def backup():
    import copy
    cfg = copy.deepcopy(store.cfg)
    cfg.pop("auth", None)
    with_secrets = request.args.get("secrets") == "1"
    if not with_secrets:
        for a, b in SECRET_PATHS:
            cfg[a][b] = ""
        for a in cfg.get("nd_accounts", []):
            a["password"] = ""
        cfg["notify"]["ntfy"]["token"] = cfg["notify"]["gotify"]["token"] = cfg["notify"]["email"]["password"] = ""
        cfg["notify"]["webhook"]["url"] = ""
    data = {"app": "ytm-sync", "version": VERSION, "exported_at": dt.datetime.now().isoformat(timespec="seconds"),
            "with_secrets": with_secrets, "config": cfg,
            "downloaded": store.state.get("downloaded", {})}
    return Response(json.dumps(data, indent=1, ensure_ascii=False), content_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             f"attachment; filename=ytm-sync_sauvegarde_{dt.datetime.now():%Y%m%d_%H%M}.json"})


@app.route("/restore", methods=["POST"])
@login_required
def restore():
    f = request.files.get("file")
    try:
        data = json.load(f)
        assert data.get("app") == "ytm-sync" and isinstance(data.get("config"), dict)
    except Exception:
        flash("Fichier de sauvegarde invalide.", "err")
        return redirect(url_for("settings") + "#backup")
    new = data["config"]
    new.pop("auth", None)
    with store.lock:
        old = store.cfg
        # secrets vides dans la sauvegarde : on garde ceux actuellement configures
        for a, b in SECRET_PATHS:
            if not (new.get(a) or {}).get(b):
                new.setdefault(a, {})[b] = old.get(a, {}).get(b, "")
        for ch, key in (("ntfy", "token"), ("gotify", "token"), ("email", "password"), ("webhook", "url")):
            if not new.get("notify", {}).get(ch, {}).get(key):
                new.setdefault("notify", {}).setdefault(ch, {})[key] = old["notify"][ch][key]
        olda = {a["user"]: a["password"] for a in old.get("nd_accounts", [])}
        for a in new.get("nd_accounts", []):
            a["password"] = a.get("password") or olda.get(a["user"], "")
        from .store import DEFAULT_CFG, _merge, validate_cfg
        merged = _merge(new, DEFAULT_CFG)
        fixed = validate_cfg(merged)
        merged["auth"] = old["auth"]
        store.cfg.clear()
        store.cfg.update(merged)
        store.save_cfg()
        if request.form.get("with_history") == "on" and isinstance(data.get("downloaded"), dict):
            store.state.setdefault("downloaded", {}).update(data["downloaded"])
            store.save_state()
    if fixed:
        flash("Réglages invalides dans le fichier, remplacés par les valeurs par défaut : " + ", ".join(fixed[:10]), "err")
    flash(f"Sauvegarde du {data.get('exported_at', '?')} restaurée ({len(merged['playlists'])} playlists, "
          f"{len(merged['ignored'])} ignorés, {len(merged['overrides'])} corrections).", "ok")
    return redirect(url_for("settings"))


@app.route("/api/apikey", methods=["POST"])
@login_required
def api_apikey():
    """La cle n'est affichee qu'une seule fois, a sa creation : seule son empreinte est enregistree."""
    act = (request.get_json(silent=True) or {}).get("action")
    sec = store.cfg["security"]
    key = None
    if act == "generate":
        key = secrets.token_urlsafe(32)
        sec.update(api_key="", api_key_hash=_key_hash(key), api_key_created=time.time())
        audit("sessions", [], "Nouvelle clé API Home Assistant générée (l'ancienne ne fonctionne plus)")
    else:
        sec.update(api_key="", api_key_hash="", api_key_created=None)
        audit("sessions", [], "API Home Assistant désactivée")
    store.save_cfg()
    return jsonify(ok=True, key=key)
