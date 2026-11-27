"""YTM Sync — point d'entree : enregistre les routes et demarre les taches de fond et le serveur web."""
import os
import signal
import threading

from waitress import serve
from . import core
from .core import app, monitor, runner, store
from .services import migrate_api_key, startup_housekeeping, status_payload
from .store import VERSION
from .sync import scheduler_loop

# Enregistrement des routes (chaque module ajoute les siennes a l'application du noyau)
from . import routes_admin, routes_api, routes_auth, routes_exports, routes_ext, routes_pages, routes_tracks  # noqa: E402
# Re-exports pratiques (tests, scripts) : app.main.app, app.main.store…
from .core import (CLEAR_MARK, FAILS, SECRET_MASK, _key_hash, _ProxyAware, safe_next)  # noqa: E402
from .services import csv_cell, find_duplicates  # noqa: E402


def _shutdown(signum, frame):
    """docker stop (SIGTERM) : etat enregistre puis arret immediat, au lieu d'etre tue au bout de 10 s."""
    print("Arrêt demandé : enregistrement de l'état…", flush=True)
    if store.lock.acquire(timeout=5):   # une ecriture en cours se termine d'abord
        try:
            store.save_state()
        except Exception as ex:
            print(f"!! État non enregistré : {ex}", flush=True)
    os._exit(0)


def main():
    from .mqtt import MqttBridge
    from .store import AUTH_F as _A, CFG_F, STATE_F, secure_file
    os.umask(0o077)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    for f in (CFG_F, STATE_F, _A):
        if os.path.exists(f):
            secure_file(f)
    startup_housekeeping()
    migrate_api_key()
    threading.Thread(target=scheduler_loop, args=(store, runner), daemon=True).start()
    threading.Thread(target=monitor.loop, daemon=True).start()
    core.mqtt_bridge = MqttBridge(store, runner, monitor, status_payload)   # partage via le noyau
    threading.Thread(target=core.mqtt_bridge.loop, daemon=True).start()
    port = int(os.environ.get("WEBUI_PORT", "8095"))
    print(f"YTM Sync v{VERSION} — interface web sur le port {port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()


__all__ = ["main", "app", "store", "runner", "monitor", "core", "CLEAR_MARK", "FAILS", "SECRET_MASK", "_key_hash",
           "_ProxyAware", "safe_next", "csv_cell", "find_duplicates", "migrate_api_key", "VERSION",
           "routes_admin", "routes_api", "routes_auth", "routes_exports", "routes_ext", "routes_pages", "routes_tracks"]
