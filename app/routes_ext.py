"""Routes : API externe pour Home Assistant (protegee par cle API)."""
from flask import jsonify, request
from .core import app, ext_auth, runner
from .services import status_payload


@app.route("/api/ext/status")
@ext_auth
def ext_status():
    return jsonify(status_payload())


@app.route("/api/ext/sync", methods=["POST"])
@ext_auth
def ext_sync():
    j = request.get_json(silent=True) or {}
    dry = j.get("dry", request.args.get("dry"))
    dry = None if dry is None else str(dry).lower() in ("1", "true", "yes", "oui")
    only = [j["playlist"]] if j.get("playlist") else None
    ok = runner.start(trigger="Home Assistant", dry=dry, only=only)
    return jsonify(ok=ok, error=None if ok else "Une synchro est déjà en cours"), (200 if ok else 409)
