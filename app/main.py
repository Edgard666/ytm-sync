"""JOAL smart controller: qBittorrent swarm conditions + JOAL orchestration.

Runs one JOAL instance per enabled tracker policy because rates are global per JOAL process.
Requires users to explicitly opt out of dryRun to enable JOAL tracker announcements.
"""
import sys
import sqlite3
import qbit_settings
import client_identity
import interface_design
import upload_history
import import_watch
import log_rotation
from collections import deque
import copy
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import os
import re
from pathlib import Path
import shutil
import tempfile
import signal
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from version import APP_VERSION
from core import watch_categories, allowed_tracker_urls, domain, domain_matches, filter_torrent, validate, ConfigError, is_complete_stopped, recorded_ratio, rule_preference, rules_missing_client, rule_active_now, schedule_label
from qbit import QBitClient
from torrent_import import ImportErrorValue, MAX_BATCH_MULTIPART_BYTES, inspect_torrent, parse_upload_batch
from solo_torrents import SoloLibrary, matching_rules, confirmed_leechers, announce_urls
from cleanup import Ledger, matches_tag, time_until
from tracker_scrape import checked_scrapes, ScrapeUnavailable, announce_category
from swarm_monitor import SwarmMonitor
from handoff import HandoffLedger, complete_running, fresh_observation, entry_confirmations_required
from diagnostics import DiagnosticsStore
from notifications import NotificationCenter
from backup_bundle import build_backup, schedule_restore, apply_pending_restore, MAX_ARCHIVE, PENDING_NAME
from web_auth import SessionManager, COOKIE_NAME

DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG = DATA / "policy.json"
JAR = Path(os.environ.get("JOAL_JAR", "/opt/joal/joal.jar"))


def _mebibytes(value, default):
    """'128m' / '1g' / '512' -> MiB; default when unreadable."""
    match = re.fullmatch(r'\s*(\d{1,6})\s*([mMgG]?)\s*', str(value or ''))
    if not match:
        return default
    return int(match.group(1)) * (1024 if match.group(2).lower() == 'g' else 1)


JOAL_HEAP_FLOOR_MB = 128     # Spring Boot + Tomcat + a few ordinary torrents
JOAL_HEAP_CEILING_MB = 1536
# Measured on JOAL 2.1.37: about 7.5 MiB of heap per MiB of .torrent metadata (29 MiB of
# metadata fail at 192m and start at 256m; 57 MiB fail at 384m, start at 448m). 12 keeps a margin.
JOAL_HEAP_BASE_MB = 64
JOAL_HEAP_PER_TORRENT_MB = 12


def joal_heap_mb(payload_bytes):
    """Heap for one JOAL session, sized on its .torrent files (v1.29.3).

    A 128 MiB fixed heap ran out of memory on a 586 GB torrent (279 787 pieces,
    about 6 MiB of metadata) once a second torrent shared the session. JOAL_XMX,
    when set, stays the minimum."""
    floor = max(JOAL_HEAP_FLOOR_MB, _mebibytes(os.environ.get("JOAL_XMX"), JOAL_HEAP_FLOOR_MB))
    needed = JOAL_HEAP_BASE_MB + JOAL_HEAP_PER_TORRENT_MB * max(0, payload_bytes) / 1048576
    size = max(floor, min(JOAL_HEAP_CEILING_MB, needed))
    return int(-(-size // 32) * 32)   # round up to 32 MiB


def _joal_java_opts(payload_bytes=0):
    """JVM options of one JOAL session. Overridable entirely with JOAL_JAVA_OPTS.

    ExitOnOutOfMemoryError: a JOAL that runs out of memory used to stay alive
    without seeding (upload "Indisponible"); it now exits and the controller
    sees the session as stopped."""
    override = os.environ.get("JOAL_JAVA_OPTS", "").split()
    if override:
        return override
    return [f"-Xmx{joal_heap_mb(payload_bytes)}m", "-XX:+UseSerialGC", "-XX:MaxMetaspaceSize=96m",
            "-XX:+ExitOnOutOfMemoryError"]


JOAL_JAVA_OPTS = _joal_java_opts()
# JOAL version built into the image (set by the Dockerfile); shown in the footer and /api/health.
JOAL_VERSION = (re.sub(r'[^0-9A-Za-z.+-]', '', os.environ.get('JOAL_VERSION', '')) or 'inconnue')[:40]
PORT = int(os.environ.get("PORT", "8788"))
PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
QBIT_URL = os.environ.get("QBIT_URL", "")
QBIT_USER = os.environ.get("QBIT_USERNAME", "")
QBIT_PASS = os.environ.get("QBIT_PASSWORD", "")
# Incremented whenever the WebUI changes the qBittorrent connection, so that
# every worker thread drops its cached client and reconnects without restart.
QBIT_GENERATION = 0
QBIT_THREADS_STARTED = False
QBIT_CONNECTION_LOCK = threading.Lock()
# Reverse proxies allowed to supply X-Forwarded-For (comma-separated IPs).
TRUSTED_PROXIES = frozenset(ip.strip() for ip in os.environ.get("TRUSTED_PROXIES", "").split(",") if ip.strip())
UPLOAD_HISTORY = deque(maxlen=3601)
STATE_LOCK = threading.RLock()
RUNNER_LOCK = threading.RLock()
LEDGER_LOCK = threading.RLock()
CLEANUP_LEDGER = None
SWARM_MONITOR = None
HANDOFF_LEDGER = None
DIAGNOSTICS = None
NOTIFICATIONS = None
SOLO_LIBRARY = None
SOLO_REVISION = 0
SOLO_EDIT_LOCK = threading.RLock()
AUTH = SessionManager(lambda: PASSWORD)
ACTIVE_HASHES = {}  # hashes currently assigned to each running JOAL process

STATUS = {"time": None, "inventoryTime": None, "evaluationTime": None,
          "error": None, "dryRun": True, "qbitConnected": False, "rules": [],
          "sample": [], "torrents": [], "solo": {"count": 0, "active": 0, "items": []},
          "totals": {"tagged": None, "selected": 0, "eligible": None, "blocked": None,
                     "displayed": 0, "evaluated": 0},
          "handoff": {"enabled": False, "cooldownMinutes": 60, "confirmations": 2, "managed": 0},
          "swarm": {"count": 0, "items": [], "source": "scrape HTTP(S)/UDP indépendant", "updatedAt": None},
          "cleanup": {"enabled": False, "deleteData": False, "source": "scrape HTTP(S)/UDP",
                      "lastCheck": None, "items": [], "error": None, "deleted": 0}}
STOP = threading.Event()
SHUTDOWN_DONE = threading.Event()
POLL_NOW = threading.Event()
SOLO_NOW = threading.Event()
CLEANUP_NOW = threading.Event()
SWARM_NOW = threading.Event()
HASH_RE = re.compile(r"^[0-9a-f]{40}$")
PENDING_DELETE = {}  # infohash -> monotonic deadline until deletion disappears from qBittorrent
RUNNERS = {}
LAST_START = {}
LOG = logging.getLogger("joal-smart")


def read_config():
    return validate(json.loads(CONFIG.read_text(encoding="utf-8")))


def ensure_config_permissions():
    """Restrict the policy file when possible; do not crash on Unraid ACL/FUSE
    mounts that disallow chmod despite granting read/write access. This file
    contains policy rules (not tracker passkeys). Never loosen permissions.
    """
    try:
        os.chmod(CONFIG, 0o600)
    except PermissionError as exc:
        if not os.access(CONFIG, os.R_OK | os.W_OK):
            raise PermissionError(
                "Permissions sur /data/policy.json insuffisantes : le conteneur "
                "doit pouvoir lire et modifier le fichier. Corriger son "
                "propriétaire/ACL depuis Unraid."
            ) from exc
        LOG.warning(
            "chmod(0600) refusé pour /data/policy.json ; fichier lisible et "
            "modifiable : démarrage maintenu. Vérifier propriétaire/ACL sur Unraid."
        )


def write_config(conf):
    """Store configuration atomically, with owner-only permissions."""
    validate(conf)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(conf, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, CONFIG)
        os.chmod(CONFIG, 0o600)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def notify_incident(key, failing, failure_message, recovery_message):
    if NOTIFICATIONS is None:
        return
    try:
        NOTIFICATIONS.observe(key, failing, failure_message, recovery_message)
    except Exception:
        LOG.warning('Échec de la persistance des notifications : %s', key)


def safe_audit(method, *args, **kwargs):
    """Telemetry may fail without altering existing qBittorrent/JOAL safety rules."""
    if DIAGNOSTICS is None:
        return
    try:
        getattr(DIAGNOSTICS, method)(*args, **kwargs)
    except Exception:
        LOG.warning("Journal diagnostic indisponible (%s)", method)


HEARTBEATS = {}  # thread name -> {"last": epoch, "restarts": n, "lastError": str|None}
STARTED_AT = time.time()
IMPORT_SCAN_SECONDS = 15


def beat(name):
    """Mark one iteration of a worker loop (shown on the health page)."""
    HEARTBEATS.setdefault(name, {"restarts": 0, "lastError": None})["last"] = time.time()


def qbit_client(client=None):
    """Return the cached client, or a new one if the connection changed."""
    if client is None or getattr(client, "generation", None) != QBIT_GENERATION:
        client = QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS)
        client.generation = QBIT_GENERATION
    return client


def supervised(target, name, on_crash=None):
    """Run a worker loop and restart it after an unexpected exception.

    Worker loops only return once STOP is set. Without supervision, a single
    unexpected error would silently and permanently disable a safety guard.
    """
    def runner():
        while not STOP.is_set():
            try:
                target()
            except Exception as exc:
                LOG.exception("Thread %s interrompu : redémarrage dans 5 s", name)
                record = HEARTBEATS.setdefault(name, {"restarts": 0, "lastError": None})
                record["restarts"] += 1
                record["lastError"] = type(exc).__name__
                record["lastErrorAt"] = time.time()
                if on_crash is not None:
                    try:
                        on_crash()
                    except Exception:
                        LOG.exception("Action de sécurité du thread %s impossible", name)
            STOP.wait(5)
    return threading.Thread(target=runner, name=name, daemon=True)


def start_qbit_threads():
    """Start qBittorrent workers once, when a connection is first configured."""
    global QBIT_THREADS_STARTED
    with QBIT_CONNECTION_LOCK:
        if QBIT_THREADS_STARTED or not QBIT_URL:
            return
        QBIT_THREADS_STARTED = True
    supervised(poll_loop, "poll", on_crash=stop_qbit_runners).start()
    supervised(guard_loop, "guard", on_crash=stop_qbit_runners).start()
    supervised(cleanup_loop, "cleanup").start()


def apply_qbit_connection():
    """Load qbit-connection.json (or Docker defaults) and apply it live."""
    global QBIT_URL, QBIT_USER, QBIT_PASS, QBIT_GENERATION
    connection = qbit_settings.effective(DATA)
    with QBIT_CONNECTION_LOCK:
        QBIT_URL, QBIT_USER, QBIT_PASS = connection['url'], connection['username'], connection['password']
        QBIT_GENERATION += 1
    if QBIT_URL:
        start_qbit_threads()
    else:
        LOG.info('Mode JOAL uniquement : aucune connexion qBittorrent configurée')
    POLL_NOW.set()
    SOLO_NOW.set()
    CLEANUP_NOW.set()


def wait_torrent_state(client, config, h, done, attempts=6, delay=1.0):
    """Re-read qBittorrent until done(torrent_or_None) is true.

    qBittorrent applies start/stop asynchronously: an immediate re-read can
    still show the previous state. Returns the last torrent seen (or None).
    """
    torrent = None
    for attempt in range(attempts):
        torrent = {t['hash']: t for t in watched_torrents(client, config)}.get(h)
        if done(torrent):
            return torrent
        if attempt + 1 < attempts and STOP.wait(delay):
            break
    return torrent


def thread_health():
    """One line per worker: age of its last loop iteration vs. its expected period."""
    try:
        conf = read_config()
        periods = {'guard': conf['guardSeconds'], 'poll': conf['refreshSeconds'],
                   'cleanup': conf['cleanup']['scrapeEveryMinutes'] * 60,
                   'swarm': conf['swarm']['intervalMinutes'] * 60}
    except Exception:
        periods = {}
    periods.update({'solo': 10, 'upload-history': 1, 'import-watch': IMPORT_SCAN_SECONDS})
    labels = {'guard': 'Garde qBittorrent', 'poll': 'Évaluation qBittorrent', 'cleanup': 'Nettoyage',
              'swarm': 'Relevés des trackers', 'solo': 'JOAL uniquement', 'upload-history': 'Historique des débits',
              'import-watch': 'Dossier surveillé'}
    qbit_threads = {'guard', 'poll', 'cleanup'}
    now = time.time()
    result = []
    for name, label in labels.items():
        record = dict(HEARTBEATS.get(name, {}))
        last = record.get('last')
        period = periods.get(name, 60)
        if last is None:
            status = 'inactif (qBittorrent non configuré)' if name in qbit_threads and not QBIT_URL else 'non démarré'
        else:
            status = 'ok' if now - last <= 2 * period + 60 else 'en retard'
        result.append({'name': name, 'label': label, 'status': status, 'periodSeconds': period,
                       'ageSeconds': None if last is None else int(now - last),
                       'restarts': record.get('restarts', 0), 'lastError': record.get('lastError')})
    return result


def storage_health():
    try:
        usage = shutil.disk_usage(DATA)
        disk = {'totalBytes': usage.total, 'freeBytes': usage.free, 'usedBytes': usage.used}
    except OSError:
        disk = None
    folder = import_dir()
    try:
        waiting = sum(1 for p in folder.iterdir() if p.is_file() and p.name.lower().endswith('.torrent'))
    except OSError:
        waiting = None
    return {'disk': disk, 'logs': log_rotation.log_files(DATA / 'runtime'),
            'logLimit': {'maxBytes': log_rotation.MAX_BYTES, 'backups': log_rotation.BACKUPS},
            'importFolder': str(folder), 'importWaiting': waiting}


def health_snapshot():
    with STATE_LOCK:
        snapshot = copy.deepcopy(STATUS)
    with RUNNER_LOCK:
        running = [{'rule': rid, 'torrents': len(ACTIVE_HASHES.get(rid, ())),
                    'uptimeSeconds': round(max(0, time.monotonic() - LAST_START.get(rid, time.monotonic())))}
                   for rid, proc in RUNNERS.items() if proc is not None and proc.poll() is None]
    last_update = snapshot.get('inventoryTime') or snapshot.get('time')
    try:
        age = max(0, int((dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_update)).total_seconds())) if last_update else None
    except (TypeError, ValueError):
        age = None
    return {'version': APP_VERSION, 'joalVersion': JOAL_VERSION, 'startedAt': dt.datetime.fromtimestamp(STARTED_AT, dt.timezone.utc).isoformat(),
            'uptimeSeconds': int(time.time() - STARTED_AT), 'qbitConfigured': bool(QBIT_URL),
            'threads': thread_health(), 'storage': storage_health(),
            'importWatch': snapshot.get('importWatch'),
            'qbitConnected': bool(snapshot.get('qbitConnected')), 'lastInventoryAt': last_update,
            'inventoryAgeSeconds': age, 'lastEvaluationAt': snapshot.get('evaluationTime'),
            'lastErrorPresent': bool(snapshot.get('error')), 'dryRun': bool(snapshot.get('dryRun')),
            'joalProcesses': running, 'activeProcesses': len(running),
            'trackerMonitorLastAt': snapshot.get('swarm', {}).get('updatedAt'),
            'trackerMonitorCount': snapshot.get('swarm', {}).get('count', 0),
            'joalTransferCounterAvailable': False}


def runner_stop(rule_id):
    """Stop the Java process before releasing its assignment.

    If termination fails, retain the process handle so a subsequent guard
    can retry and so no replacement can be started by accident.
    Caller must hold RUNNER_LOCK.
    """
    proc = RUNNERS.get(rule_id)
    if proc is not None and rule_id.startswith('solo-') and SWARM_MONITOR is not None:
        try:
            live_joal_upload()
            SWARM_MONITOR.flush_peers()
        except (ValueError, OSError, TypeError):
            LOG.warning("Dernier relevé JOAL indisponible")
    if proc is not None and proc.poll() is None:
        LOG.info("Arrêt de JOAL pour la règle %s", rule_id)
        proc.terminate()
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if proc is not None and proc.poll() is None:
        raise RuntimeError(f"Processus JOAL {rule_id} toujours actif après tentative d'arrêt")
    # A session ended only if the process has actually exited.
    if proc is not None:
        save_final_volumes(rule_id, proc)
        safe_audit('stop_rule', rule_id)
    ACTIVE_HASHES.pop(rule_id, None)
    RUNNERS.pop(rule_id, None)


def stop_all():
    """Attempt all stops and report whether all JOAL processes are gone."""
    success = True
    with RUNNER_LOCK:
        for key in list(RUNNERS):
            try:
                runner_stop(key)
            except Exception:
                success = False
                LOG.exception("Impossible d'arrêter JOAL pour %s", key)
        if any(proc.poll() is None for proc in RUNNERS.values()):
            success = False
    return success


def stop_qbit_runners():
    """On qBittorrent API failure leave independent solo monitors/processes alone."""
    success = True
    with RUNNER_LOCK:
        for key in list(RUNNERS):
            if key.startswith('solo-'):
                continue
            try:
                runner_stop(key)
            except Exception:
                success = False
                LOG.exception('Impossible d’arrêter le processus qBittorrent %s', key)
    return success


def stop_solo_runners():
    with RUNNER_LOCK:
        for key in list(RUNNERS):
            if key.startswith('solo-'):
                try:
                    runner_stop(key)
                except Exception:
                    LOG.exception('Arrêt JOAL uniquement non confirmé (%s)', key)


def simulations_paused():
    return (DATA / "simulations.paused").exists()


def set_simulations_paused(paused):
    marker = DATA / "simulations.paused"
    with RUNNER_LOCK:
        if paused:
            marker.touch(mode=0o600, exist_ok=True)
            for proc in RUNNERS.values():
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
            if not stop_all():
                raise RuntimeError("Arrêt incomplet. Réessayer la pause.")
        else:
            marker.unlink(missing_ok=True)
    POLL_NOW.set()


TORRENT_MODES = {}
FORCED_RULE = ":forced"


FORCED_IDLE = ':forced-idle'
FORCED_IDS = {FORCED_RULE, FORCED_IDLE}
FORCED_RUNNERS = FORCED_IDS | {'solo-' + rid for rid in FORCED_IDS}


def idle_assignments(config):
    idle_ids = {r['id'] for r in config['rules'] if r['enabled']
                and r.get('minUploadRate') == r.get('maxUploadRate') == 0}
    return {h for h, rid in config.get('torrentRuleOverrides', {}).items() if rid in idle_ids}


def simulation_config(config):
    with RUNNER_LOCK:
        modes = TORRENT_MODES
    # Forced keeps a zero rate only for a torrent explicitly assigned to a 0/0 rule;
    # otherwise it uses the real rates of the first client profile.
    idle = idle_assignments(config)
    forced = {h: FORCED_IDLE if h in idle else FORCED_RULE for h, mode in modes.items() if mode == 'forced'}
    rules = list(config['rules'])
    profiles = [r for r in rules if r.get('client')]
    if FORCED_RULE in forced.values() and profiles:
        profile = next((r for r in profiles if r['enabled']), profiles[0])
        rules.append(dict(profile, id=FORCED_RULE, enabled=True, trackerDomains=[], minLeechers=0,
                          minLeechersByHash={}, targetRatio=0, simultaneousSeed=config['maxCandidates'],
                          schedule=None))  # Forced ignores time windows
    if FORCED_IDLE in forced.values():
        rules.append(dict(id=FORCED_IDLE, enabled=True, trackerDomains=[], minLeechers=0,
                          minLeechersByHash={}, targetRatio=0, simultaneousSeed=config['maxCandidates'],
                          minUploadRate=0, maxUploadRate=0, client=None))
    return dict(config, rules=rules, simulationModes=modes,
                torrentRuleOverrides={**config.get('torrentRuleOverrides', {}), **forced})


def set_torrent_modes(hashes, mode):
    global TORRENT_MODES
    with RUNNER_LOCK:
        updated = dict(TORRENT_MODES)
        for h in hashes:
            if mode == 'auto':
                updated.pop(h, None)
            else:
                updated[h] = mode
        tmp = DATA / 'simulation-modes.tmp'
        tmp.write_text(json.dumps(updated), encoding='utf-8')
        tmp.replace(DATA / 'simulation-modes.json')
        TORRENT_MODES = updated
        for rid, assigned in list(ACTIVE_HASHES.items()):
            if not (assigned & hashes):
                continue
            forced = rid in FORCED_RUNNERS
            if mode == 'stop' or forced != (mode == 'forced'):
                runner_stop(rid)
                LAST_START.pop(rid, None)
        if mode == 'forced':
            for rid in FORCED_RUNNERS:
                LAST_START.pop(rid, None)
    # Switching to an active mode: fresh seeders/leechers now instead of waiting
    # for the regular scrape rotation ("Dernier relevé trop ancien").
    if mode in ('auto', 'forced') and SWARM_MONITOR is not None:
        if SWARM_MONITOR.request_refresh(hashes):
            SWARM_NOW.set()
    POLL_NOW.set()
    SOLO_NOW.set()


def presence_profile(rule):
    """.client profile for a zero-rate rule: its own, else the first rule with one.

    Same choice as Forced mode (first enabled rule with a client, else first
    rule with a client). Returns None when no usable profile file exists.
    """
    candidates = [rule.get("client")]
    try:
        rules = read_config()["rules"]
        candidates += [r.get("client") for r in rules if r.get("enabled")]
        candidates += [r.get("client") for r in rules]
    except (OSError, ValueError, ConfigError):
        pass
    for name in candidates:
        if name and (DATA / "clients" / name).is_file():
            return name
    return None


_IDENTITY_WARNED = set()


def probe_identity(_infohash=None):
    """Client identity for the controller's stopped announces: the profile JOAL would use
    for a zero-rate session (first enabled rule with a .client). None -> generic request."""
    name = presence_profile({'client': None})
    if not name:
        return None
    try:
        return client_identity.load(DATA / 'clients' / name)
    except (OSError, client_identity.Unsupported) as exc:
        if name not in _IDENTITY_WARNED:
            _IDENTITY_WARNED.add(name)
            LOG.warning('Profil %s non reproductible pour les relevés (%s) : requête générique', name, exc)
        return None


def runner_start(rule, selected):
    if simulations_paused():
        raise RuntimeError("Pause globale activée")
    if STOP.is_set():
        raise RuntimeError("Arrêt du contrôleur en cours : lancement JOAL interdit")
    rid = rule["id"]
    expected = 'forced' if rid in FORCED_RUNNERS else 'auto'
    if any(TORRENT_MODES.get(h, 'auto') != expected for h in selected):
        raise RuntimeError("Mode du torrent modifié : réévaluation en cours")
    if rid in RUNNERS and RUNNERS[rid].poll() is None:
        raise RuntimeError(f"Processus JOAL déjà actif pour {rid}")
    if any(proc.poll() is None and ACTIVE_HASHES.get(key, set()).intersection(selected)
           for key, proc in RUNNERS.items()):
        raise RuntimeError("Torrent déjà en simulation dans un autre processus")
    if rid in RUNNERS:
        save_final_volumes(rid, RUNNERS[rid])
    root = DATA / "runtime" / rid
    torrent_dir = root / "torrents"
    torrent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(torrent_dir, 0o700)
    # All inactive files have been removed after stopping previous JVM.
    for old in torrent_dir.glob("*.torrent"):
        old.unlink()
    for hash_, payload in selected.items():
        target = torrent_dir / (hash_ + ".torrent")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
        os.chmod(target, 0o600)
    presence_only = rule["minUploadRate"] == rule["maxUploadRate"] == 0
    # Zero-rate rules also run through JOAL when a .client profile exists, so
    # the tracker sees an emulated, whitelisted client (peer_id, key,
    # User-Agent). The bare Python script is only a fallback without profile:
    # private trackers usually reject its announces.
    presence_client = presence_profile(rule) if presence_only else None
    use_joal = not presence_only or presence_client is not None
    if use_joal:
        clients = root / "clients"
        if clients.is_symlink() and clients.resolve() != (DATA / "clients").resolve():
            clients.unlink()
        if not clients.exists():
            clients.symlink_to(DATA / "clients", target_is_directory=True)
        if not (DATA / "clients").is_dir():
            raise RuntimeError("Dossier /data/clients absent : extraire les fichiers .client de JOAL")
        client_files = sorted((DATA / "clients").glob("*.client"))
        if not client_files:
            raise RuntimeError("Aucun fichier .client présent dans /data/clients")
        # No implicit client identity: an accidental mismatch could cause unwanted tracker behavior.
        client_name = presence_client or rule.get("client")
        if not client_name:
            raise RuntimeError(f"Règle {rid}: renseigner 'client' avec un .client de /data/clients")
        if not (DATA / "clients" / client_name).is_file():
            raise RuntimeError(f"Client JOAL introuvable : {client_name}")
        joal_conf = {
            "minUploadRate": rule["minUploadRate"],
            "maxUploadRate": rule["maxUploadRate"],
            "simultaneousSeed": min(rule["simultaneousSeed"], len(selected)),
            "client": client_name,
            "keepTorrentWithZeroLeechers": True,
            "uploadRatioTarget": -1.0,
        }
        (root / "config.json").write_text(json.dumps(joal_conf, indent=2), encoding="utf-8")
        os.chmod(root / "config.json", 0o600)
    telemetry = tempfile.TemporaryDirectory(prefix="joal-upload-")
    telemetry_path = telemetry.name + "/upload.json"
    if not use_joal:
        LOG.warning("Règle %s : aucun profil .client, annonces de présence sans émulation de client "
                    "(souvent refusées par les trackers privés)", rid)
        command = [sys.executable, str(Path(__file__).resolve().with_name("seed_presence.py")),
                   str(torrent_dir), telemetry_path]
    else:
        java_opts = _joal_java_opts(sum(len(payload) for payload in selected.values()))
        LOG.info("JOAL %s : %d torrent(s), mémoire %s", rid, len(selected), java_opts[0] if java_opts else "?")
        command = ["java", *java_opts, "-Dcontroller.telemetry=" + telemetry_path,
                   "-jar", str(JAR), f"--joal-conf={root}"]
    # Output goes through a pipe so joal.log can rotate (LOG_MAX_MB x LOG_BACKUPS).
    proc = None
    log = log_rotation.RotatingLog(root / "joal.log")
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=root,
                                start_new_session=True)
        RUNNERS[rid] = proc
        proc.telemetry = telemetry
        ACTIVE_HASHES[rid] = set(selected)
        proc.presence_only = presence_only
        proc.engine = "joal" if use_joal else "script"
        proc.volume_session = uuid.uuid4().hex
        if proc.stdout is not None:
            log_rotation.start_pump(proc.stdout, log)
        else:
            log.close()
    except Exception:
        log.close()
        if proc is not None:
            if proc.stdout is not None:
                proc.stdout.close()
            runner_stop(rid)
        raise
    LAST_START[rid] = time.monotonic()
    safe_audit('start_rule', rid, sorted(selected))
    LOG.info("JOAL démarré pour %s : %d torrents", rid, len(selected))


def torrent_added_timestamp(torrent):
    """qBittorrent torrents/info `added_on`: Unix seconds, -1 if unknown."""
    value = torrent.get("added_on")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 32503680000:
        return int(value)
    return None


def real_upload_progress(torrent, target_ratio):
    """Measured qBittorrent upload, never JOAL's simulated announcement counter.

    Unknown counters must remain null. A ratio target is a visual comparison,
    not a claim that JOAL announced or a tracker accepted any data.
    """
    def count(name):
        value = torrent.get(name)
        return value if type(value) is int and 0 <= value <= 10**19 else None

    uploaded = count("uploaded")
    if uploaded is None:
        uploaded = count("total_uploaded")
    size = count("size")
    if not size:
        size = count("total_size")
    if not size:
        size = None
    try:
        target = float(target_ratio) if target_ratio is not None else 0.0
    except (ValueError, TypeError, OverflowError):
        target = 0.0
    if not (0 < target <= 100):
        target = 0.0
    target_bytes = round(size * target) if size is not None and target else None
    percent = (round(min(100.0, max(0.0, uploaded * 100.0 / target_bytes)), 1)
               if uploaded is not None and target_bytes else None)
    total_size = count("total_size")
    return {"uploadedBytes": uploaded, "sizeBytes": size,
            "contentSizeBytes": total_size if total_size is not None else count("size"),
            "targetBytes": target_bytes, "percent": percent,
            "source": "qBittorrent (transfert réel uniquement)"}


def live_joal_sessions():
    """Active JVM sessions and uptime; no fabricated per-torrent upload volume."""
    result = {}
    with RUNNER_LOCK:
        for rid, process in RUNNERS.items():
            if process is None or process.poll() is not None:
                continue
            started = LAST_START.get(rid)
            seconds = max(0, round(time.monotonic() - started)) if started is not None else None
            for infohash in ACTIVE_HASHES.get(rid, set()):
                result[infohash] = {"active": True, "rule": rid,
                                     "uptimeSeconds": seconds, "simulatedBytes": None}
    return result


def volume_samples(rid, proc, sample):
    session = getattr(proc, 'volume_session', None)
    if not isinstance(session, str) or not isinstance(sample, dict):
        return []
    return [(h, session, row[1]) for h, row in sample.items()
            if h in ACTIVE_HASHES.get(rid, ()) and isinstance(row, list) and len(row) >= 2
            and type(row[1]) is int and 0 <= row[1] < 2**63]


def read_announce_outcomes(proc):
    """Last JOAL announce result per infohash, as fixed public labels only."""
    try:
        with (Path(proc.telemetry.name) / "announce.json").open() as stream:
            raw = json.load(stream)
    except (AttributeError, OSError, ValueError):
        return {}
    result = {}
    if not isinstance(raw, dict):
        return result
    for infohash, hosts in raw.items():
        if not isinstance(infohash, str) or not HASH_RE.fullmatch(infohash):
            continue
        if isinstance(hosts, list):  # Telemetry from the previous release.
            hosts = {'': hosts}
        if not isinstance(hosts, dict):
            continue
        for host, row in hosts.items():
            if (not re.fullmatch(r'[a-zA-Z0-9.:\[\]-]{0,253}', host)
                    or not isinstance(row, list) or len(row) < 3 or type(row[1]) is not int):
                continue
            result.setdefault(infohash, {})[host or 'Moteur JOAL'] = {
                'result': announce_category(row[0] == 1, row[2]), 'time': row[1]}
    return result


def announce_issue_rows(snapshot):
    """Show the latest refused announce directly in the row's reason."""
    announcements = snapshot.get("joalUpload", {}).get("announcements", {})
    for row in [*snapshot.get('torrents', []), *snapshot.get('solo', {}).get('items', [])]:
        outcomes = announcements.get(row.get('infoHash'))
        if not outcomes:
            continue
        latest = max(outcomes.values(), key=lambda item: item.get('time', 0))
        row['lastAnnounce'] = latest
        if all(item.get('result') != 'Acceptée' for item in outcomes.values()):
            row['reason'] = 'Annonce refusée : ' + str(latest.get('result'))


def upload_hint(rate, peers, zero_rate):
    """Why an active JOAL session simulates no upload, from JOAL's own tracker view.

    JOAL gives a torrent no bandwidth when the tracker reports no leecher, or no
    seeder other than JOAL itself (PeersAwareWeightCalculator)."""
    if zero_rate or rate != 0 or not isinstance(peers, dict):
        return None
    seeders, leechers = peers.get('seeders'), peers.get('leechers')
    if leechers == 0:
        return 'Upload à 0 : aucun leecher signalé par le tracker'
    if seeders == 0:
        return 'Upload à 0 : aucun autre seeder signalé (JOAL n’uploade qu’avec au moins un autre seeder)'
    return None


def upload_hint_rows(snapshot):
    upload = snapshot.get('joalUpload', {})
    rates, peers = upload.get('byInfoHash', {}), upload.get('peers', {})
    zero_rate = set(upload.get('zeroRate', ()))
    for row in [*snapshot.get('torrents', []), *snapshot.get('solo', {}).get('items', [])]:
        h = row.get('infoHash')
        if h in rates:
            hint = upload_hint(rates[h], peers.get(h), h in zero_rate)
            if hint:
                row['uploadHint'] = hint


def save_final_volumes(rid, proc):
    if not isinstance(getattr(proc, 'volume_session', None), str):
        return
    try:
        with (Path(proc.telemetry.name) / "upload.json").open() as stream:
            rows = volume_samples(rid, proc, json.load(stream))
        upload_history.session_volumes(DATA / 'upload-totals.sqlite', rows)
    except FileNotFoundError:
        pass  # Process ended before publishing its first sample.
    except (OSError, ValueError, sqlite3.Error):
        LOG.exception("Dernier compteur d’envoi JOAL indisponible")


def live_joal_upload(*, include_totals=True):
    """Collect all live runner rates before any UI filtering or pagination."""
    rates = {}
    peers = {}
    peer_times = {}
    announcements = {}
    volumes = {}
    zero_rate = set()
    total = 0
    missing = 0
    measured = []
    checkpoints = []
    with RUNNER_LOCK:
        for rid, proc in RUNNERS.items():
            if proc is None or proc.poll() is not None:
                continue
            try:
                path = Path(proc.telemetry.name) / "upload.json"
                with path.open() as stream:
                    fresh = time.time() - os.fstat(stream.fileno()).st_mtime < 10
                    sample = json.load(stream) if fresh else None
                if sample is not None and not isinstance(sample, dict):
                    sample = None
            except (AttributeError, OSError, ValueError):
                sample = None
            if getattr(proc, 'engine', None) == 'joal':
                for infohash, outcome in read_announce_outcomes(proc).items():
                    if infohash in ACTIVE_HASHES.get(rid, ()):
                        announcements[infohash] = outcome
            if sample is None:
                missing += 1
            else:
                samples = volume_samples(rid, proc, sample)
                if samples:
                    recorded = getattr(proc, 'recorded_volumes', {})
                    changed = [entry for entry in samples if entry[2] > recorded.get(entry[0], -1)]
                    if changed:
                        measured.extend(changed)
                        checkpoints.append((proc, recorded, changed))
                total += sum(row[0] for row in sample.values()
                             if isinstance(row, list) and row and type(row[0]) is int)
            if getattr(proc, 'presence_only', False) is True:
                zero_rate.update(ACTIVE_HASHES.get(rid, ()))
            for infohash in ACTIVE_HASHES.get(rid, ()):
                values = sample.get(infohash, (0, 0)) if sample is not None else (None, None)
                if not isinstance(values, (list, tuple)) or len(values) < 2:
                    values = (None, None)
                rate, volume = values[:2]
                if len(values) >= 6 and isinstance(values[5], dict) and getattr(proc, 'presence_only', False) is True:
                    announcements[infohash] = values[5]
                if len(values) >= 4:
                    peers[infohash] = {key: value for key, value in zip(("seeders", "leechers"), values[2:])
                                       if type(value) is int and value >= 0}
                    if len(values) >= 5 and type(values[4]) is int:
                        peer_times[infohash] = values[4]
                    if SWARM_MONITOR is not None and peers[infohash]:
                        if len(values) >= 5 and type(values[4]) is int and values[4] > 0:
                            SWARM_MONITOR.remember_peers(infohash, peers[infohash], values[4], 'JOAL')
                        else:
                            # Some JOAL samples carry counts before their announce timestamp.
                            # Date the first local reading of each change for display only.
                            previous = SWARM_MONITOR.display_peers([infohash]).get(infohash, {})
                            changed = {key: count for key, count in peers[infohash].items()
                                       if previous.get(key, {}).get('count') != count}
                            SWARM_MONITOR.remember_peers(infohash, changed, int(time.time()), 'Lecture locale JOAL')
                rates[infohash] = rate
                volumes[infohash] = volume
        try:
            cumulative = upload_history.session_volumes(DATA / 'upload-totals.sqlite', measured,
                                                        totals=include_totals)
            for proc, recorded, changed in checkpoints:
                recorded.update((h, count) for h, _, count in changed)
                proc.recorded_volumes = recorded
        except (sqlite3.Error, OSError):
            LOG.exception("Cumul des volumes JOAL indisponible")
            cumulative = None
    return {"totalUploadedBytes": cumulative, "bytesPerSecond": None if missing else total, "byInfoHash": rates, "uploadedBytes": volumes, "peers": peers, "peerTimes": peer_times, "announcements": announcements,
            "zeroRate": sorted(zero_rate)}


def upload_history_loop():
    pending = deque(maxlen=3601)
    next_commit = time.monotonic() + 30
    path = DATA / "upload-history.sqlite"
    while not STOP.is_set():
        beat("upload-history")
        try:
            rate = live_joal_upload(include_totals=False)["bytesPerSecond"]
        except Exception:
            LOG.debug("Débit JOAL illisible", exc_info=True)
            rate = None
        now = int(time.time())
        with STATE_LOCK:
            UPLOAD_HISTORY.append((now, rate))
        pending.append((now, rate))
        stopped = STOP.wait(1)
        if stopped or time.monotonic() >= next_commit:
            next_commit = time.monotonic() + 30
            try:
                if SWARM_MONITOR is not None:
                    SWARM_MONITOR.flush_peers()
                upload_history.store(path, list(pending))
                pending.clear()
            except (OSError, sqlite3.Error):
                LOG.warning("Historique des débits : écriture indisponible")
    if pending:
        try:
            upload_history.store(path, list(pending))
        except (OSError, sqlite3.Error):
            LOG.warning("Historique des débits : dernière écriture indisponible")



def safe_tracker_details(trackers):
    """Public tracker inventory, including all HTTP(S)/UDP hosts, never URLs/passkeys."""
    details = {}
    for tracker in trackers:
        raw = tracker.get("url")
        if not isinstance(raw, str):
            continue
        try:
            parsed = urlparse(raw)
            scheme = parsed.scheme.lower()
            if scheme not in ("http", "https", "udp"):
                continue
            host = (parsed.hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if not host:
            continue
        key = (host, scheme)
        seeders = tracker.get("num_seeds")
        seeders = seeders if type(seeders) is int and seeders >= 0 else None
        leechers = tracker.get("num_leeches")
        leechers = leechers if type(leechers) is int and leechers >= 0 else None
        record = details.setdefault(key, {"domain": host, "protocol": scheme,
                                          "status": None, "leechers": None, "seeders": None})
        status = tracker.get("status")
        if type(status) is int:
            if status == 2 or record["status"] is None:
                record["status"] = status
        if seeders is not None:
            record["seeders"] = max(record["seeders"] or 0, seeders)
        if leechers is not None:
            record["leechers"] = max(record["leechers"] or 0, leechers)
    return [details[key] for key in sorted(details)]


def check_item(label, result, detail=""):
    return {"label": label, "result": result, "detail": str(detail)}


def build_rule_checks(config, rule, relevant, complete, safe, leechers, source,
                      threshold, ratio, target, cooldown, pending):
    """Explain the decision without converting unknown values into zeros."""
    values = [
        watch_check(config),
        check_item("Règle active", True, rule["id"]),
        check_item("Tracker couvert par la règle", bool(relevant),
                   ", ".join(sorted({host for host, _ in relevant})) if relevant else "Aucun domaine correspondant"),
        check_item("Torrent complet", complete, "100 % requis"),
        check_item("Torrent arrêté dans qBittorrent", safe,
                   "État arrêté et contenu intégral requis"),
        check_item("Contrôle des leechers", True if threshold == 0 else leechers >= 0,
                   "Désactivé : seuil 0 (valeur inconnue non convertie en zéro)" if threshold == 0
                   else (f"{leechers} · {source}" if leechers >= 0 else "Inconnu — relevé requis")),
        check_item("Seuil de leechers atteint", True if threshold == 0 else (None if leechers < 0 else leechers >= threshold),
                   "Sans condition sur le nombre de leechers" if threshold == 0
                   else f"{leechers if leechers >= 0 else 'Inconnu'} / minimum {threshold}"),
        check_item("Ratio local non atteint", None if target and ratio is None else not bool(target and ratio is not None and ratio >= target),
                   f"{ratio:.2f} / cible {target}" if ratio is not None and target else "Pas de limite ou ratio inconnu"),
        check_item("Temporisation terminée", cooldown <= 0,
                   f"{int((cooldown + 59) / 60)} min restantes" if cooldown else "Aucune temporisation"),
        check_item("Aucune suppression en cours", not pending),
    ]
    return values


def watch_value(config):
    mode = config.get("qbitFilterMode", "tag")
    if mode == "tag":
        return config["qbitTag"]
    categories = ", ".join(watch_categories(config))
    if mode == "either":
        return f"Tag {config['qbitTag']} ou catégorie parmi {categories}"
    return categories


def watch_check(config):
    label = {"category": "Catégorie de surveillance", "either": "Tag ou catégorie de surveillance"}.get(
        config.get("qbitFilterMode"), "Tag de surveillance")
    return check_item(label, True, watch_value(config))


def matches_watch(torrent, config, categories=None):
    mode = config.get("qbitFilterMode", "tag")
    if mode == "tag":
        return matches_tag(torrent, config["qbitTag"])
    if matches_tag(torrent, config["completedTag"]):
        return False
    if categories is None:
        categories = watch_categories(config)
    return (torrent.get("category") in categories
            or (mode == "either" and matches_tag(torrent, config["qbitTag"])))


def watched_torrents(client, config):
    if config.get("qbitFilterMode") == "either":
        categories = set(watch_categories(config))
        return [torrent for torrent in client.torrents_all()
                if torrent.get("hash") and matches_watch(torrent, config, categories)]
    if config.get("qbitFilterMode") == "category":
        selected = {}
        for name in watch_categories(config):
            for torrent in client.torrents(name, field="category"):
                if torrent.get("category") == name and not matches_tag(torrent, config["completedTag"]):
                    selected[torrent["hash"]] = torrent
        return list(selected.values())
    return client.torrents(config["qbitTag"])


def find_candidates(client, config):
    """Evaluate ALL tagged torrents then rank eligible ones globally.

    maxCandidates remains in policy.json for v1.8 compatibility, but now caps
    *selected sessions*, not the size of the inventory. A torrent can be
    selected at most once, even when it matches multiple rules.
    """
    torrents = watched_torrents(client, config)
    for stale_hash in list(PENDING_DELETE):
        if PENDING_DELETE[stale_hash] <= time.monotonic():
            PENDING_DELETE.pop(stale_hash, None)
    active_rules = {r["id"]: r for r in config["rules"] if r["enabled"]}
    selection = {rid: {} for rid in active_rules}
    preview = []
    completions = set()
    candidates = []
    # Independent scrape observations are preferred when recent. Never turn an
    # unavailable or stale scrape into a fabricated zero.
    swarm_observations = {}
    if SWARM_MONITOR is not None and config.get("swarm", {}).get("enabled", True):
        snapshot = SWARM_MONITOR.snapshot(config["swarm"]["maxAgeMinutes"] * 60)
        swarm_observations = {
            entry["infoHash"]: entry.get("rules", {})
            for entry in snapshot.get("items", [])
        }

    overrides = config.get("torrentRuleOverrides", {})
    for torrent in torrents:
        h = torrent["hash"]
        trackers = client.trackers(h)
        tracker_details = safe_tracker_details(trackers)
        observed_hosts = sorted({d["domain"] for d in tracker_details
                                 if d["protocol"] in ("http", "https", "udp")})
        name = str(torrent.get("name") or "(sans nom)")[:160]
        progress = float(torrent.get("progress", 0))
        complete = progress >= 1.0
        safe = is_complete_stopped(torrent)
        ratio = recorded_ratio(torrent)
        # An old qBittorrent swarm count can be shown to the user, but never
        # used to authorize a JOAL handoff, rank as eligible, or delete data.
        historic_counts = [int(tr["num_leeches"]) for tr in trackers
                           if type(tr.get("num_leeches")) is int and tr["num_leeches"] >= 0]
        fallback_qbit = max(historic_counts, default=-1)
        if fallback_qbit < 0:
            count = torrent.get("num_incomplete")
            if type(count) is int and count >= 0:
                fallback_qbit = count
        qb_seeders = torrent.get('num_complete')
        qb_seeders = qb_seeders if type(qb_seeders) is int and qb_seeders >= 0 else None
        tracker_seeds = [d['seeders'] for d in tracker_details if type(d.get('seeders')) is int]
        if tracker_seeds:
            qb_seeders = max(tracker_seeds)
        first_row = len(preview)
        manual_rule = overrides.get(h.lower())
        for rule in config["rules"]:
            if rule['id'] in FORCED_IDS and manual_rule != rule['id']:
                continue
            if not rule["enabled"] or (manual_rule and rule['id'] != manual_rule):
                continue
            threshold = rule.get("minLeechersByHash", {}).get(h.lower(), rule["minLeechers"])
            relevant = []
            for tracker in trackers:
                url = tracker.get("url")
                host = domain(url) if isinstance(url, str) else None
                if host and (rule['id'] == manual_rule or any(domain_matches(host, d) for d in rule["trackerDomains"])):
                    relevant.append((host, tracker))
            valid = [(host, tr) for host, tr in relevant
                     if tr.get("status") == 2 and type(tr.get("num_leeches")) is int
                     and tr["num_leeches"] >= 0]
            observed = swarm_observations.get(h.lower(), {}).get(rule["id"], {})
            # A partial scrape still provides a valid measurement for the
            # responding tracker(s); never describe it as a global swarm total.
            scrape_valid = observed.get("status") == "récent" or (
                observed.get("status") == "partiel" and
                type(observed.get("successCount")) is int and observed["successCount"] > 0)
            scraped = observed.get("leechers") if scrape_valid else None
            partial_scrape = observed.get("leechers") if observed.get("status") == "partiel" else None
            sampled_at = observed.get("sampledAt") if observed.get("status") in ("récent", "partiel") else None
            if type(scraped) is int and scraped >= 0 and relevant:
                leechers = scraped
                if observed.get("status") == "partiel":
                    source = (f"scrape partiel frais ({observed.get('successCount')}/"
                              f"{observed.get('trackerCount', '?')} trackers répondants)")
                else:
                    source = "scrape autonome récent"
                matches = ([tr["url"] for _, tr in relevant if isinstance(tr.get("url"), str)]
                           if leechers >= threshold else [])
                matches = list(dict.fromkeys(matches))
            else:
                leechers = max((tr["num_leeches"] for _, tr in valid), default=-1)
                source = "qBittorrent (valeur éventuellement en cache)" if leechers >= 0 else "inconnu"
                # Explicit assignment is scoped to this torrent; do not change
                # global domain rules used for other torrents.
                effective_rule = (dict(rule, trackerDomains=observed_hosts)
                                  if manual_rule == rule['id'] else rule)
                matches = allowed_tracker_urls(trackers, effective_rule, threshold)
            # Zero is an explicit opt-out of the leecher threshold, not a
            # pretend scrape value. Keep requiring an actual matching tracker.
            if threshold == 0 and relevant:
                matches = list(dict.fromkeys(
                    tr['url'] for _, tr in relevant
                    if isinstance(tr.get('url'), str) and
                    tr['url'].lower().startswith(('http://', 'https://', 'udp://'))))
            target = rule.get("targetRatio", 0)
            target_met = bool(target and ratio is not None and ratio >= target)
            cooling = (HANDOFF_LEDGER.cooldown_remaining(h) if HANDOFF_LEDGER is not None
                       and config.get("handoff", {}).get("enabled") else 0)
            eligible = safe and bool(matches) and not target_met and h not in PENDING_DELETE and cooling == 0
            if not complete:
                reason = "Torrent incomplet"
            elif not safe:
                reason = "Torrent actif dans qBittorrent (arrêt obligatoire)"
            elif not relevant:
                reason = ("Tracker non ciblé : domaine différent de la règle" if observed_hosts
                          else "Aucun tracker HTTP(S)/UDP identifiable")
            elif target_met:
                reason = "Ratio local atteint (en attente de changement de tag)" if config["dryRun"] else "Ratio local atteint"
                completions.add(h)
            elif threshold > 0 and leechers < 0 and SWARM_MONITOR is not None and SWARM_MONITOR.refresh_pending(h):
                reason = "Relevé des trackers en cours…"
            elif threshold > 0 and leechers < 0:
                reason = "Tracker inactif ou nombre de leechers inconnu"
            elif not matches:
                reason = ("Aucun tracker HTTP(S) compatible avec le moteur JOAL" if threshold == 0
                          else f"Leechers insuffisants : {leechers} / minimum {threshold}")
            elif cooling:
                reason = f"Priorité qBittorrent : temporisation {int((cooling + 59) / 60)} min"
            elif PENDING_DELETE.get(h, 0) > time.monotonic():
                reason = "Suppression du torrent en cours dans qBittorrent"
            else:
                reason = ("Éligible — aucun contrôle du seuil de leechers (minimum 0)" if threshold == 0
                          else "Éligible — classement des torrents en cours")
            mode = config.get('simulationModes', {}).get(h, 'auto')
            if mode == 'stop':
                eligible, reason = False, 'Stop manuel'
                completions.discard(h)
            elif mode == 'forced' and rule['id'] not in FORCED_IDS:
                eligible, reason = False, 'Forced en attente de profil JOAL'
                completions.discard(h)
            elif eligible and not rule_active_now(rule):
                eligible, reason = False, 'Hors plage horaire (' + schedule_label(rule) + ')'
            current_seeders = observed.get("seeders") if scrape_valid else None
            current_seeders = current_seeders if type(current_seeders) is int and current_seeders >= 0 else None
            row = {
                "seeders": current_seeders,
                "displaySeeders": current_seeders if current_seeders is not None else qb_seeders,
                "seederSource": ("scrape autonome récent" if current_seeders is not None
                                 else "qBittorrent non daté" if qb_seeders is not None else "inconnu"),
                "name": name, "rule": rule["id"], "eligible": eligible,
                "ruleOverride": manual_rule, "ruleAssignment": "manuel" if manual_rule else "automatique",
                "selected": False, "reason": reason,
                "leechers": leechers, "threshold": threshold, "leecherSource": source,
                "displayLeechers": leechers if leechers >= 0 else (partial_scrape if type(partial_scrape) is int else (fallback_qbit if fallback_qbit >= 0 else None)),
                "displaySource": source if leechers >= 0 else ("scrape partiel · données non validées" if type(partial_scrape) is int else ("qBittorrent · valeur non datée, informative seulement" if fallback_qbit >= 0 else "inconnu")),
                "leecherSampledAt": sampled_at,
                "selectedTrackers": len(matches), "trackers": observed_hosts,
                "trackerDetails": tracker_details,
                "matchedTrackers": sorted({host for host, _ in relevant}),
                "checks": build_rule_checks(config, rule, relevant, complete, safe,
                    leechers, source, threshold, ratio, target, cooling,
                    PENDING_DELETE.get(h, 0) > time.monotonic()),
                "ruleEnabled": True,
                "hash": h[:12], "infoHash": h, "progress": min(100, max(0, round(progress * 100))),
                "qbitState": str(torrent.get("state", "inconnu"))[:40],
                "ratio": ratio, "targetRatio": target, "upload": real_upload_progress(torrent, target), "stopped": safe,
                "addedOn": torrent_added_timestamp(torrent),
                "priorityRank": None,
                "handoff": HANDOFF_LEDGER.public_status(h) if HANDOFF_LEDGER else {},
            }
            preview.append(row)
            if eligible:
                candidates.append((leechers, name.casefold(), h, rule["id"], matches, row))
        if len(preview) == first_row:
            disabled_matches = []
            for r in config["rules"]:
                if r["enabled"]:
                    continue
                matched = [(host, tr) for tr in trackers
                           for host in [domain(tr.get("url", ""))]
                           if host and any(domain_matches(host, d) for d in r["trackerDomains"])]
                if matched and not manual_rule:
                    disabled_matches.append((r, matched))
            if disabled_matches and not manual_rule:
                candidate, matched = disabled_matches[0]
                rule_id = candidate["id"]
                threshold = candidate.get("minLeechersByHash", {}).get(h.lower(), candidate["minLeechers"])
                target = candidate.get("targetRatio", 0)
                checked = [tr["num_leeches"] for _, tr in matched
                           if tr.get("status") == 2 and type(tr.get("num_leeches")) is int
                           and tr["num_leeches"] >= 0]
                leechers = max(checked, default=-1)
                reason = f"Règle « {rule_id} » désactivée — activer dans Règles & trackers"
            else:
                rule_id, threshold, target = "", None, None
                checked = [tr["num_leeches"] for tr in trackers
                           if tr.get("status") == 2 and type(tr.get("num_leeches")) is int
                           and tr["num_leeches"] >= 0 and domain(tr.get("url", ""))]
                leechers = max(checked, default=-1)
                if manual_rule:
                    reason = f"Règle manuelle « {manual_rule} » absente ou désactivée"
                    rule_id = manual_rule
                elif not config["rules"]:
                    reason = "Aucune règle configurée — ouvrir Règles & trackers"
                elif observed_hosts:
                    reason = "Aucune règle active ne correspond à ce tracker"
                else:
                    reason = "Aucun tracker HTTP(S)/UDP détecté et aucune règle active"
            preview.append({
                "seeders": None, "displaySeeders": qb_seeders,
                "seederSource": "qBittorrent non daté" if qb_seeders is not None else "inconnu",
                "name": name, "rule": rule_id, "ruleEnabled": False, "eligible": False,
                "ruleOverride": manual_rule, "ruleAssignment": "manuel" if manual_rule else "automatique",
                "selected": False, "reason": reason,
                "leechers": leechers, "threshold": threshold, "leecherSource": "qBittorrent" if leechers >= 0 else "inconnu",
                "displayLeechers": leechers if leechers >= 0 else (fallback_qbit if fallback_qbit >= 0 else None),
                "displaySource": "qBittorrent non daté" if leechers < 0 and fallback_qbit >= 0 else ("qBittorrent" if leechers >= 0 else "inconnu"),
                "selectedTrackers": 0, "trackers": observed_hosts,
                "trackerDetails": tracker_details,
                "matchedTrackers": sorted({host for host, _ in disabled_matches[0][1]}) if disabled_matches else [],
                "checks": ([
                    watch_check(config),
                    check_item("Règle active", False, rule_id),
                    check_item("Tracker couvert par la règle", True, ", ".join(sorted({host for host, _ in disabled_matches[0][1]}))),
                    check_item("Torrent complet", complete, "100 % requis"),
                    check_item("Torrent arrêté dans qBittorrent", safe),
                    check_item("Nombre de leechers disponible", leechers >= 0,
                        str(leechers) if leechers >= 0 else "Inconnu"),
                ] if disabled_matches else [
                    watch_check(config),
                    check_item("Règle applicable au tracker", False,
                               ", ".join(observed_hosts) if observed_hosts else "Aucun tracker HTTP(S)/UDP exploitable"),
                    check_item("Torrent complet", complete, "100 % requis"),
                    check_item("Torrent arrêté dans qBittorrent", safe),
                    check_item("Nombre de leechers disponible", leechers >= 0,
                               str(leechers) if leechers >= 0 else "Inconnu"),
                ]),
                "hash": h[:12], "infoHash": h,
                "progress": min(100, max(0, round(progress * 100))),
                "qbitState": str(torrent.get("state", "inconnu"))[:40],
                "ratio": ratio, "targetRatio": target, "upload": real_upload_progress(torrent, target), "stopped": safe,
                "addedOn": torrent_added_timestamp(torrent),
                "priorityRank": None,
                "handoff": HANDOFF_LEDGER.public_status(h) if HANDOFF_LEDGER else {},
            })

    # Evaluate every matching rule, then choose just one per infohash. Sort
    # applicable rules by the effective threshold, never by JSON list order.
    # If that rule's quota is exhausted, try the next applicable rule without
    # exceeding either the global cap or a per-rule cap.
    grouped = {}
    for candidate in candidates:
        grouped.setdefault(candidate[2], []).append(candidate)
    priority = sorted(grouped.items(), key=lambda pair: (
        config.get('simulationModes', {}).get(pair[0]) != 'forced',
        not _manual_joal_priority(pair[0]),
        -max(item[0] for item in pair[1]),
        min(item[1] for item in pair[1]), pair[0]))
    ranked = {h: index for index, (h, _) in enumerate(priority, 1)}
    global_count = 0
    for h, choices in priority:
        choices.sort(key=lambda item: rule_preference(active_rules[item[3]], h))
        chosen = None
        for count, _, _, rid, matches, row in choices:
            if global_count >= config['maxCandidates']:
                row['reason'] = f"Limite globale de {config['maxCandidates']} torrents simultanés atteinte"
                row['checks'].append(check_item('Place disponible globalement', False, row['reason']))
                continue
            if len(selection[rid]) >= active_rules[rid]['simultaneousSeed']:
                row['reason'] = f'Limite de la règle {rid} atteinte'
                row['checks'].append(check_item('Place disponible dans la règle', False, row['reason']))
                continue
            if not config['dryRun']:
                raw = client.export(h)
                selection[rid][h] = filter_torrent(raw, matches, h)
            else:
                selection[rid][h] = b''
            global_count += 1
            row['selected'] = True
            row['reason'] = f'Retenu : {count} leechers (seuil {row["threshold"]}, règle {rid})'
            row['checks'].append(check_item('Place disponible', True, f'Sélection : {rid}'))
            chosen = row
            break
        for _, _, _, rid, _, row in choices:
            if row is not chosen and chosen is not None:
                row['reason'] = f'Autre règle applicable : « {chosen["rule"]} » préférée'
                row['checks'].append(check_item('Une seule règle par torrent', True,
                                               f'Sélection : {chosen["rule"]}'))
    for row in preview:
        row['priorityRank'] = ranked.get(row['infoHash'])
    return selection, preview, len(torrents), completions


def compact_dashboard_rows(preview):
    """One dashboard row per tagged torrent, even with multiple tracker rules."""
    grouped = {}
    for row in preview:
        key = row.get("infoHash") or row.get("hash")
        current = grouped.get(key)
        # Selected > eligible > matching tracker rule > any unrelated rule.
        threshold = row.get('threshold')
        if type(threshold) is not int:
            threshold = float('inf')
        target = row.get('targetRatio') or float('inf')
        score = (int(bool(row.get('selected'))),
                 int(bool(row.get('eligible'))),
                 int(bool(row.get('matchedTrackers'))),
                 -threshold, target)
        if current is None or score > current[0]:
            grouped[key] = (score, dict(row))
    return [value[1] for value in grouped.values()]


def _display_totals(tagged, rows):
    eligible = sum(1 for row in rows if row.get("eligible"))
    return {"tagged": tagged,
            "selected": sum(1 for row in rows if row.get("selected")),
            "eligible": eligible,
            "blocked": max(0, tagged - eligible),
            "displayed": min(len(rows), 1500),
            "evaluated": len(rows)}


def update_inventory_snapshot(torrents):
    """Refresh the tag inventory promptly without querying trackers or exporting files.

    The tracker evaluation is intentionally separate and slower. When an
    inventory item changes state, previous eligibility becomes unknown until
    an evaluation is completed. This avoids presenting stale decisions.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    joal_sessions = live_joal_sessions()
    by_hash = {torrent["hash"]: torrent for torrent in torrents if isinstance(torrent.get("hash"), str) and torrent["hash"]}
    with STATE_LOCK:
        previous = {row.get("infoHash"): row for row in STATUS.get("torrents", []) if row.get("infoHash")}
        cleanup_by_hash = {item.get("infoHash"): item for item in STATUS.get("cleanup", {}).get("items", [])}
        old_ids = set(previous)
        next_rows = []
        changed = old_ids != set(by_hash)
        for hash_, torrent in by_hash.items():
            row = dict(previous.get(hash_, {}))
            progress = torrent.get("progress", 0)
            try:
                progress_pct = max(0, min(100, round(float(progress) * 100)))
            except (ValueError, TypeError, OverflowError):
                progress_pct = 0
            stopped = is_complete_stopped(torrent)
            torrent_state = str(torrent.get("state", "inconnu"))[:40]
            ratio = recorded_ratio(torrent)
            state_changed = (not row or row.get("qbitState") != torrent_state or
                             row.get("progress") != progress_pct or row.get("stopped") != stopped or
                             (row.get("targetRatio") not in (None, 0) and row.get("ratio") != ratio))
            if state_changed:
                changed = True
                row.update(selected=False, eligible=False,
                           checks=[check_item("Torrent complet", progress_pct >= 100),
                                   check_item("Torrent arrêté dans qBittorrent", stopped),
                                   check_item("Nouvelle évaluation des règles", None, "En attente")],
                           reason=("Torrent incomplet" if progress_pct < 100 else
                                   "Torrent actif dans qBittorrent (arrêt obligatoire)" if not stopped else
                                   "Arrêté — vérification des règles en attente"))
            row["cleanup"] = cleanup_by_hash.get(hash_, {})
            row["joalSession"] = joal_sessions.get(hash_, {"active": False, "simulatedBytes": None})
            if HANDOFF_LEDGER is not None:
                row["handoff"] = HANDOFF_LEDGER.public_status(hash_)
                remaining = row["handoff"].get("cooldownRemainingSeconds", 0)
                if remaining and not row.get("selected"):
                    row["reason"] = f"Priorité qBittorrent : attente encore {int((remaining + 59) / 60)} min"
            row.update({"name": str(torrent.get("name") or "(sans nom)")[:160],
                        "infoHash": hash_, "hash": hash_[:12],
                        "qbitState": torrent_state, "progress": progress_pct,
                        "ratio": ratio, "stopped": stopped,
                        "upload": real_upload_progress(torrent, row.get("targetRatio")),
                        "addedOn": torrent_added_timestamp(torrent)})
            row.setdefault("rule", "")
            row.setdefault("leechers", -1)
            row.setdefault("threshold", None)
            row.setdefault("targetRatio", None)
            row.setdefault("trackers", [])
            row.setdefault("trackerDetails", [])
            row.setdefault("checks", [check_item("Évaluation des règles", None, "En attente")])
            row.setdefault("selected", False)
            row.setdefault("eligible", False)
            next_rows.append(row)
        next_rows.sort(key=lambda row: (not row["joalSession"]["active"], row["name"].casefold()))
        STATUS.update(time=now, inventoryTime=now, qbitConnected=True,
                      torrents=next_rows[:1500],
                      totals=_display_totals(len(by_hash), next_rows))
        if str(STATUS.get("error") or "").startswith("Surveillance rapide indisponible"):
            STATUS["error"] = None
    return changed


def _swarm_by_hash(config):
    if not SWARM_MONITOR or not config["swarm"]["enabled"]:
        return {}
    snap = SWARM_MONITOR.snapshot(config["swarm"]["maxAgeMinutes"] * 60)
    return {entry["infoHash"]: entry.get("rules", {}) for entry in snap.get("items", [])}


def _resume_owned(client, config, hashes, reason):
    """Return only torrents stopped *by us* to qBittorrent. Never start manual stops.

    If JOAL cannot be stopped, do NOT start qBittorrent: report the error.
    We also stop the whole JOAL rule if that process contains one hash.
    """
    for h in sorted(set(hashes)):
        if HANDOFF_LEDGER is None or not HANDOFF_LEDGER.entry(h).get('owned'):
            continue
        with RUNNER_LOCK:
            for rid, assigned in list(ACTIVE_HASHES.items()):
                if h in assigned:
                    runner_stop(rid)
        current = {t['hash']: t for t in watched_torrents(client, config)}
        torrent = current.get(h)
        if torrent is None:
            # It may have been removed or untagged. Do not start a torrent
            # that we can no longer identify safely.
            HANDOFF_LEDGER.mark_returned(h, config['handoff']['cooldownMinutes'],
                                         reason + ' : torrent non disponible')
            continue
        if torrent.get('state') in ('stoppedUP', 'pausedUP'):
            client.start_torrent(h)
            fresh = wait_torrent_state(client, config, h,
                lambda t: t is None or t.get('state') not in ('stoppedUP', 'pausedUP'))
            if fresh is not None and fresh.get('state') in ('stoppedUP', 'pausedUP'):
                raise RuntimeError('Reprise qBittorrent non confirmée : ' + h[:12])
        HANDOFF_LEDGER.mark_returned(h, config['handoff']['cooldownMinutes'], reason)
        LOG.info('Priorité qBittorrent restaurée pour %s : %s', h[:12], reason)


def _handle_manual_qbit_requests(client, config, by_hash):
    """Process explicit user return requests on the single poll worker.

    The user action authorizes starting a previously manually stopped torrent,
    but only when its files are complete. It never bypasses stopping JOAL.
    No files or torrent tags are deleted.
    """
    if HANDOFF_LEDGER is None:
        return
    for h, request in sorted(HANDOFF_LEDGER.pending_manual().items()):
        if request.get('target') != 'qbit':
            continue
        t = by_hash.get(h)
        if t is None:
            HANDOFF_LEDGER.finish_manual(h, 'qbit', 'rejected', 'Torrent suivi introuvable')
            continue
        if t.get('progress', 0) != 1 or t.get('amount_left', 0) != 0:
            HANDOFF_LEDGER.finish_manual(h, 'qbit', 'rejected', 'Torrent incomplet : opération annulée')
            continue
        # One JOAL process may host multiple torrents. Stop the process before
        # qBittorrent is permitted to start this torrent, even if not owned.
        with RUNNER_LOCK:
            for rid, assigned in list(ACTIVE_HASHES.items()):
                if h in assigned:
                    runner_stop(rid)  # If stop fails, DO NOT start qBittorrent.
        if t.get('state') in ('stoppedUP', 'pausedUP'):
            client.start_torrent(h)
            fresh = wait_torrent_state(client, config, h,
                lambda item: item is not None and item.get('state') not in ('stoppedUP', 'pausedUP'))
            if fresh is None or fresh.get('state') in ('stoppedUP', 'pausedUP'):
                raise RuntimeError('Redémarrage qBittorrent non confirmé : ' + h[:12])
        # Mark the 60-minute qBittorrent priority even if the torrent already
        # ran; this prevents the automatic scheduler undoing the manual choice.
        HANDOFF_LEDGER.mark_returned(h, config['handoff']['cooldownMinutes'],
                                     'Retour manuel : priorité qBittorrent')
        HANDOFF_LEDGER.finish_manual(h, 'qbit', 'completed',
                                    'Retour qBittorrent confirmé (temporisation active)')
        LOG.info('Demande manuelle : qBittorrent prioritaire pour %s', h[:12])


def _manual_joal_priority(h):
    """Pending JOAL switch can win ranking, never skip eligibility rules."""
    item = HANDOFF_LEDGER.manual_request(h) if HANDOFF_LEDGER else None
    return bool(item and item.get('target') == 'joal' and item.get('status') == 'pending')


def _finish_running_joal_requests(config):
    """Only claim completion after a JOAL JVM is actually running."""
    if not HANDOFF_LEDGER:
        return
    if config['dryRun']:
        return
    with RUNNER_LOCK:
        assigned = set().union(*(set(ACTIVE_HASHES.get(rid, set()))
                                  for rid, proc in RUNNERS.items() if proc.poll() is None))
    for h, req in HANDOFF_LEDGER.pending_manual().items():
        if req['target'] == 'joal' and h in assigned:
            HANDOFF_LEDGER.finish_manual(h, 'joal', 'completed',
                                         'Processus JOAL démarré et qBittorrent arrêté')


def _reconcile_handoff(client, config):
    """Conservative qBit-first transitions, never based on cached leecher counts.

    The user must explicitly enable automatic management. Two distinct fresh
    tracker scrapes (default) are required before stopping real seeding.
    Only originally auto-stopped torrents may be auto-restarted.
    """
    if HANDOFF_LEDGER is None or STOP.is_set():
        return
    if getattr(HANDOFF_LEDGER, 'corrupt', False):
        raise RuntimeError('Fichier handoff-state.json illisible : gestion automatique suspendue')
    opts = config['handoff']
    torrent_list = watched_torrents(client, config)
    by_hash = {t['hash']: t for t in torrent_list}
    owned = HANDOFF_LEDGER.owned_hashes()
    if HANDOFF_LEDGER.recovery_pending:
        # A restarted controller has lost every previous JOAL process. Restore
        # real seeding rather than assuming phantom processes are still alive.
        _resume_owned(client, config, owned, 'Reprise sûre après redémarrage du contrôleur')
        HANDOFF_LEDGER.recovery_pending = False
        # A pending manual qBittorrent request can still be processed on the
        # next poll; restart recovery always takes priority.
        return
    _handle_manual_qbit_requests(client, config, by_hash)
    # Manual return may have changed the torrent state: never reuse a stale
    # snapshot for automatic JOAL selection or launch.
    torrent_list = watched_torrents(client, config)
    by_hash = {t['hash']: t for t in torrent_list}
    if not opts['enabled'] or config['dryRun'] or simulations_paused():
        for h, req in HANDOFF_LEDGER.pending_manual().items():
            if req['target'] == 'joal':
                HANDOFF_LEDGER.finish_manual(h, 'joal', 'rejected',
                    'Gestion automatique désactivée ou dryRun activé')
        _resume_owned(client, config, owned, 'Basculement désactivé : priorité qBittorrent')
        return
    # If a manually stopped torrent was started in qBittorrent after the
    # request, the user's most recent qBittorrent action takes precedence.
    for h, req in HANDOFF_LEDGER.pending_manual().items():
        if (req['target'] == 'joal' and req.get('initialState') == 'stopped'
                and h in by_hash and complete_running(by_hash[h])):
            HANDOFF_LEDGER.mark_returned(h, opts['cooldownMinutes'],
                                         'Reprise manuelle qBittorrent pendant la demande JOAL')
            HANDOFF_LEDGER.finish_manual(h, 'joal', 'rejected',
                                         'qBittorrent relancé manuellement : priorité conservée')
    observations = _swarm_by_hash(config)
    rules = [r for r in config['rules'] if r['enabled']]
    current_owned = HANDOFF_LEDGER.owned_hashes()
    to_return = set()
    for h in sorted(current_owned):
        t = by_hash.get(h)
        if t and complete_running(t):
            # A qBittorrent restart takes priority. NEVER release the ownership
            # record until the conflicting JOAL process has been stopped.
            with RUNNER_LOCK:
                for runner_id, assigned in list(ACTIVE_HASHES.items()):
                    if h in assigned:
                        runner_stop(runner_id)
            HANDOFF_LEDGER.mark_returned(h, opts['cooldownMinutes'],
                                         'qBittorrent a repris : priorité au partage réel')
            HANDOFF_LEDGER.finish_manual(h, 'joal', 'rejected',
                                         'Reprise qBittorrent détectée : priorité au partage réel')
            continue
        if not t or not is_complete_stopped(t):
            # Unknown state: stop JOAL; do not start a file in an ambiguous state.
            to_return.add(h)
            continue
        if TORRENT_MODES.get(h) == 'stop':
            to_return.add(h)
            continue
        if TORRENT_MODES.get(h) == 'forced':
            continue
        rid = HANDOFF_LEDGER.entry(h).get('rule')
        manual_rule = config.get('torrentRuleOverrides', {}).get(h)
        rule = next((r for r in rules if r['id'] == rid), None)
        if rule is None or (manual_rule and manual_rule != rid):
            to_return.add(h)
            continue
        try:
            tracker_hosts = [domain(tr.get('url', '')) for tr in client.trackers(h)]
        except Exception:
            to_return.add(h)
            continue
        if not any(host and (manual_rule == rid or any(domain_matches(host, pat) for pat in rule['trackerDomains']))
                   for host in tracker_hosts):
            to_return.add(h)
            continue
        observed = fresh_observation(observations, h, rid)
        if observed is None:
            to_return.add(h)  # cannot confirm demand: fail back to real sharing
            continue
        threshold = rule.get('minLeechersByHash', {}).get(h.lower(), rule['minLeechers'])
        consecutive = HANDOFF_LEDGER.observe(h, rid, observed, threshold)
        if observed[0] < threshold and consecutive >= opts['confirmations']:
            to_return.add(h)
    if to_return:
        _resume_owned(client, config, to_return,
                      'Tracker indisponible, règle modifiée ou leechers sous le seuil')
    # Rank the *entire* tagged inventory. A running torrent is eligible for
    # automatic stop only when a fresh scrape exists. All manually stopped
    # torrents still occupy potential capacity, avoiding a stop > maxCandidates.
    rank = []
    for t in torrent_list:
        h = t['hash']
        if TORRENT_MODES.get(h) in ('stop', 'forced'):
            continue
        if not (is_complete_stopped(t) or complete_running(t)):
            continue
        if HANDOFF_LEDGER.cooldown_remaining(h):
            continue
        try:
            tracker_hosts = [domain(tr.get('url', '')) for tr in client.trackers(h)]
        except Exception:
            continue
        chosen_id = config.get('torrentRuleOverrides', {}).get(h)
        for rule in rules:
            if chosen_id and rule['id'] != chosen_id:
                continue
            if not any(host and (chosen_id == rule['id'] or any(domain_matches(host, pat) for pat in rule['trackerDomains']))
                       for host in tracker_hosts):
                continue
            rid = rule['id']
            obs = fresh_observation(observations, h, rid)
            if obs is None:
                continue
            leecher, _ = obs
            threshold = rule.get('minLeechersByHash', {}).get(h.lower(), rule['minLeechers'])
            if leecher < threshold:
                HANDOFF_LEDGER.observe(h, rid, obs, threshold)
                continue
            ratio = recorded_ratio(t)
            if rule.get('targetRatio') and ratio is not None and ratio >= rule['targetRatio']:
                continue
            # Only the first match of this infohash will be used below.
            rank.append((-leecher, str(t.get('name', '')).casefold(), h, rid, t, rule, obs, threshold))
    rank.sort(key=lambda v: (not _manual_joal_priority(v[2]), v[0], v[1], v[2], v[3]))
    chosen = set()
    per_rule = {}
    for _, _, h, rid, torrent, rule, obs, threshold in rank:
        if h in chosen or len(chosen) >= config['maxCandidates']:
            continue
        if per_rule.get(rid, 0) >= rule['simultaneousSeed']:
            continue
        chosen.add(h)
        per_rule[rid] = per_rule.get(rid, 0) + 1
        if not complete_running(torrent):
            continue
        consecutive = HANDOFF_LEDGER.observe(h, rid, obs, threshold)
        # A clear surplus (minimum + margin) needs only one real fresh scrape.
        # Normal threshold crossings still need distinct samples; all other
        # protections (cooldown, priority, quotas, state) remain mandatory.
        required = entry_confirmations_required(obs[0], threshold,
                    opts['confirmations'], opts.get('fastTrackMargin', 5))
        if consecutive < required:
            continue
        # Re-read the actual torrent state just before requesting a stop.
        current = {t['hash']: t for t in watched_torrents(client, config)}
        if h not in current or not complete_running(current[h]):
            continue
        if STOP.is_set() or simulations_paused():
            return
        # Persist ownership BEFORE asking qBittorrent to stop. If the API
        # times out after accepting the request, recovery can still restore
        # real seeding rather than abandoning a stopped torrent.
        HANDOFF_LEDGER.mark_stopped_by_controller(h, rid)
        try:
            client.stop_torrent(h)
            stopped = wait_torrent_state(client, config, h,
                lambda t: t is None or is_complete_stopped(t))
            if stopped is not None and is_complete_stopped(stopped):
                LOG.info('qBittorrent arrêté après contrôles confirmés pour %s', h[:12])
            else:
                LOG.warning('Arrêt qBittorrent non confirmé pour %s ; restauration prioritaire', h[:12])
                _resume_owned(client, config, {h}, 'Arrêt qBittorrent non confirmé')
        except Exception:
            LOG.exception("Échec de transition vers l'arrêt : restauration requise pour %s", h[:12])
            _resume_owned(client, config, {h}, 'Erreur de basculement qBittorrent')
            raise
    for h, req in HANDOFF_LEDGER.pending_manual().items():
        if req['target'] == 'joal' and h not in by_hash:
            HANDOFF_LEDGER.finish_manual(h, 'joal', 'rejected',
                                         'Torrent absent ou hors du filtre qBittorrent')
    HANDOFF_LEDGER.remove_absent(set(by_hash))


def run_once(client):
    if STOP.is_set():
        return
    config = read_config()
    _reconcile_handoff(client, config)
    config = simulation_config(config)
    selection, preview, tagged_total, completions = find_candidates(client, config)
    # qBittorrent keeps priority over standalone JOAL for the shared limit.
    # Stop entire standalone rule processes as required (a process can host
    # several torrents), then proceed with the real-sharing selections.
    wanted_qbit = set().union(*(set(v) for v in selection.values()))
    with RUNNER_LOCK:
        solo_ids = [rid for rid, proc in RUNNERS.items()
                    if rid.startswith('solo-') and proc.poll() is None]
        running_solo = sum(len(ACTIVE_HASHES.get(rid, ())) for rid in solo_ids)
        for rid in solo_ids:
            if len(wanted_qbit) + running_solo <= config['maxCandidates']:
                break
            running_solo -= len(ACTIVE_HASHES.get(rid, ()))
            runner_stop(rid)
    # Metadata discovery is NOT done inside the 5s qBittorrent guard: a slow
    # tracker API call must never delay stopping an active JOAL process.
    if SWARM_MONITOR is not None:
        try:
            if SWARM_MONITOR.synchronize(watched_torrents(client, config),
                                         client.trackers, watch_value(config)):
                SWARM_NOW.set()
        except Exception:
            LOG.warning("Synchronisation des métadonnées tracker différée")
    # The JOAL runner is per rule, not per torrent. Kill any old process
    # before changing tags, then reevaluate selected hashes for safety.
    if completions and not config["dryRun"]:
        with RUNNER_LOCK:
            for rid in list(RUNNERS):
                if ACTIVE_HASHES.get(rid, set()) & completions:
                    runner_stop(rid)
        # Idempotent: add the completion tag first, then remove the source tag.
        # In case of partial failure a later poll retries; no data is deleted.
        for hash_ in sorted(completions):
            client.add_tag(hash_, config["completedTag"])
            if config.get("qbitFilterMode", "tag") == "tag":
                client.remove_tag(hash_, config["qbitTag"])
    now = time.monotonic()
    rules_state = []
    desired_ids = set(selection)
    with RUNNER_LOCK:
        for rid in list(RUNNERS):
            if rid.startswith('solo-'):
                continue
            if rid not in desired_ids or config["dryRun"] or not selection.get(rid):
                runner_stop(rid)
    for rule in config["rules"]:
        rid = rule["id"]
        if not rule["enabled"]:
            rules_state.append({"id": rid, "state": "désactivée", "eligible": 0})
            continue
        selected = selection[rid]
        fingerprint = hashlib.sha256(b"".join(h.encode() + hashlib.sha256(payload).digest()
                       for h, payload in sorted(selected.items())) + json.dumps(rule, sort_keys=True).encode()).hexdigest()
        root = DATA / "runtime" / rid
        marker = root / "selected.sha256"
        previous = marker.read_text().strip() if marker.is_file() else None
        with RUNNER_LOCK:
            proc = RUNNERS.get(rid)
            alive = proc is not None and proc.poll() is None
        if simulations_paused():
            state = "pause globale"
        elif config["dryRun"]:
            state = "simulation locale (aucune annonce)"
        elif not selected:
            with RUNNER_LOCK:
                runner_stop(rid)
            state = ("hors plage horaire · " + schedule_label(rule) if not rule_active_now(rule)
                     else "aucun torrent admissible")
        elif alive and fingerprint == previous:
            state = "en cours"
        else:
            if alive:
                with RUNNER_LOCK:
                    runner_stop(rid)
            # Avoid hammering trackers with reannounces after frequent swarm changes.
            elapsed = now - LAST_START.get(rid, -999999)
            if elapsed < 900:
                failure = runner_failure_text(rid)
                state = (f"{failure} · nouvelle tentative dans {int(900-elapsed)} s" if failure
                         else f"attente anti-relance ({int(900-elapsed)} s)")
            else:
                try:
                    # Recheck immediately before launch; the regular guard also
                    # checks for unexpected reactivations of qBittorrent.
                    fresh = {t["hash"]: t for t in watched_torrents(client, config)}
                    if not all(h in fresh and is_complete_stopped(fresh[h]) for h in selected):
                        raise RuntimeError("Un torrent a repris dans qBittorrent : lancement annulé")
                    with RUNNER_LOCK:
                        runner_start(rule, selected)
                    root.mkdir(parents=True, exist_ok=True)
                    marker.write_text(fingerprint)
                    state = "démarrée"
                except Exception as exc:
                    LOG.warning("Démarrage impossible pour %s: %s", rid, exc)
                    state = f"erreur : {str(exc)[:100]}"
        rules_state.append({"id": rid, "eligible": len(selected), "state": state,
                            "minLeechers": rule["minLeechers"], "schedule": schedule_label(rule),
                            "rate": f"{rule['minUploadRate']}–{rule['maxUploadRate']} kB/s"})
    _finish_running_joal_requests(config)
    if HANDOFF_LEDGER is not None:
        with RUNNER_LOCK:
            assigned = set().union(*(set(ACTIVE_HASHES.get(rid, set()))
                                     for rid, proc in RUNNERS.items() if proc.poll() is None))
        orphaned = HANDOFF_LEDGER.owned_hashes() - assigned
        if orphaned:
            _resume_owned(client, config, orphaned,
                          'JOAL ne prend pas en charge le torrent : retour qBittorrent')
    joal_sessions = live_joal_sessions()
    with STATE_LOCK:
        ordered_rows = sorted(compact_dashboard_rows(preview),
                              key=lambda r: (r["infoHash"] not in joal_sessions, r["name"].casefold()))
        now_string = dt.datetime.now(dt.timezone.utc).isoformat()
        snapshots = {item["infoHash"]: item for item in STATUS.get("cleanup", {}).get("items", [])}
        for row in ordered_rows:
            row["joalSession"] = joal_sessions.get(row.get("infoHash"), {"active": False, "simulatedBytes": None})
            row["cleanup"] = snapshots.get(row.get("infoHash"), {})
            if HANDOFF_LEDGER is not None:
                row["handoff"] = HANDOFF_LEDGER.public_status(row["infoHash"])
                remaining = row["handoff"].get("cooldownRemainingSeconds", 0)
                if remaining and not row["selected"]:
                    row["reason"] = f"Priorité qBittorrent : attente encore {int((remaining + 59) / 60)} min"
        STATUS["handoff"] = dict(config["handoff"], managed=(len(HANDOFF_LEDGER.owned_hashes()) if HANDOFF_LEDGER else 0))
        STATUS.update(time=now_string, evaluationTime=now_string, error=None,
                      qbitConnected=True, dryRun=config["dryRun"], rules=rules_state,
                      sample=preview[:100], torrents=ordered_rows[:1500],
                      totals=_display_totals(tagged_total, ordered_rows))
    safe_audit('observe', ordered_rows)


def qbit_inventory_hashes(client, peer_counts=None):
    """Unfiltered inventory. None means that absence cannot be verified."""
    try:
        inventory = client.torrents_all()
        if not isinstance(inventory, list):
            return None
        hashes = set()
        for torrent in inventory:
            if not isinstance(torrent, dict) or not re.fullmatch(
                    r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})', str(torrent.get('hash', ''))):
                return None
            primary = torrent['hash'].lower()
            hashes.add(primary)
            if peer_counts is not None:
                peer_counts[primary] = {key: torrent[field] for key, field in
                                        (('seeders', 'num_complete'), ('leechers', 'num_incomplete'))
                                        if type(torrent.get(field)) is int and torrent[field] >= 0}
            v1 = torrent.get('infohash_v1')
            if isinstance(v1, str) and HASH_RE.fullmatch(v1.lower()):
                hashes.add(v1.lower())
                if peer_counts is not None:
                    peer_counts[v1.lower()] = peer_counts[primary]
        return hashes
    except (OSError, RuntimeError, ValueError):
        return None


def import_probe_allowed(h):
    # A running JOAL session already reports this torrent's counts: no extra announce.
    with RUNNER_LOCK:
        if any(proc.poll() is None and h in ACTIVE_HASHES.get(rid, set()) for rid, proc in RUNNERS.items()):
            return False
    if not QBIT_URL:
        return True
    hashes = qbit_inventory_hashes(QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS))
    return hashes is not None and h not in hashes


def forget_torrent_settings(hashes):
    """Drop the per-torrent settings of removed library torrents (v1.30.2): assigned rule,
    per-torrent leecher threshold and Stop/Forced mode. Kept when the same torrent is in
    qBittorrent (they still apply there) or when qBittorrent cannot be checked."""
    global TORRENT_MODES
    hashes = set(hashes)
    if not hashes:
        return
    if QBIT_URL:
        inventory = qbit_inventory_hashes(QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS))
        if inventory is None:
            return
        hashes -= set(inventory)
    if not hashes:
        return
    try:
        with STATE_LOCK:
            conf = read_config()
            changed = False
            overrides = conf.get('torrentRuleOverrides', {})
            for h in hashes & set(overrides):
                del overrides[h]
                changed = True
            for rule in conf.get('rules', []):
                thresholds = rule.get('minLeechersByHash') or {}
                for h in hashes & set(thresholds):
                    del thresholds[h]
                    changed = True
            if changed:
                write_config(conf)
        with RUNNER_LOCK:
            if hashes & set(TORRENT_MODES):
                updated = {h: mode for h, mode in TORRENT_MODES.items() if h not in hashes}
                tmp = DATA / 'simulation-modes.tmp'
                tmp.write_text(json.dumps(updated), encoding='utf-8')
                tmp.replace(DATA / 'simulation-modes.json')
                TORRENT_MODES = updated
    except (OSError, ValueError, ConfigError):
        LOG.warning('Réglages des torrents retirés non nettoyés (configuration illisible ou non enregistrable)')


def add_to_library(payload, filename, conf, source='Interface web'):
    """Store one .torrent in the JOAL-only library and register it everywhere.

    Shared by the WebUI import and the watched folder. Raises ImportErrorValue
    (duplicate or invalid file) or OSError (write refused).
    """
    with SOLO_EDIT_LOCK:
        info = SOLO_LIBRARY.add(payload, conf['rules'], filename=filename)
        pending = {'infoHash': info['hash'], 'hash': info['hash'][:12], 'name': info['name'],
                   'fileName': info['fileName'], 'sizeBytes': info.get('sizeBytes'),
                   'trackers': info['domains'], 'selected': False, 'active': False,
                   'reason': 'Importé · première évaluation en cours'}
        publish_solo_edit(info['hash'], pending)
    if SWARM_MONITOR is not None:
        SWARM_MONITOR.register_solo(info['hash'], info['name'], announce_urls(payload), info.get('sizeBytes'))
        SWARM_NOW.set()
    safe_audit('event', 'solo.import', 'Torrent ajouté à JOAL uniquement (' + source + ')', infohash=info['hash'])
    return info, pending


def import_dir():
    configured = os.environ.get('IMPORT_DIR', '').strip()
    return Path(configured) if configured else DATA / 'import'


def watched_import(payload, filename):
    """Importer used by import_watch.scan(): returns (status, message)."""
    try:
        info, _ = add_to_library(payload, filename, read_config(), source='dossier surveillé')
    except ImportErrorValue as exc:
        duplicate = 'déjà dans la bibliothèque' in str(exc)
        return ('duplicate' if duplicate else 'error'), str(exc)
    except OSError:
        return 'error', 'Écriture refusée dans la bibliothèque /data/torrents'
    return 'added', 'Importé : ' + info['name']


def import_watch_loop():
    folder = import_dir()
    try:
        import_watch.ensure(folder)
    except OSError:
        LOG.warning('Dossier d’import %s inaccessible : import automatique désactivé', folder)
    recent = []
    observations = {}
    while not STOP.is_set():
        beat("import-watch")
        if SOLO_LIBRARY is not None and folder.is_dir():
            results = import_watch.scan(folder, watched_import, observations=observations)
            for item in results:
                LOG.info('Dossier surveillé : %s → %s (%s)', item['filename'], item['status'], item['message'])
            if results:
                stamp = dt.datetime.now(dt.timezone.utc).isoformat()
                recent = ([dict(item, at=stamp) for item in reversed(results)] + recent)[:30]
                if any(item['status'] == 'added' for item in results):
                    SOLO_NOW.set()
                    POLL_NOW.set()
            with STATE_LOCK:
                STATUS['importWatch'] = {'folder': str(folder), 'recent': recent,
                                         'lastScan': dt.datetime.now(dt.timezone.utc).isoformat()}
        STOP.wait(IMPORT_SCAN_SECONDS)


def publish_solo_edit(h, row=None):
    """Make a committed library edit visible independently of tracker evaluation."""
    global SOLO_REVISION
    with STATE_LOCK:
        SOLO_REVISION += 1
        solo = STATUS.setdefault('solo', {})
        items = solo.setdefault('items', [])
        previous = next((item for item in items if item['infoHash'] == h), None)
        if previous:
            items.remove(previous)
        if row is not None:
            items.append(row)
        solo['count'] = max(0, solo.get('count', 0) + (1 if row is not None else -1))
        if previous and previous.get('active'):
            solo['active'] = max(0, solo.get('active', 0) - 1)
    SOLO_NOW.set()


def latest_torrent_count(h, observations, saved_peers, cutoff, now):
    """Freshest single valid leecher count for one standalone torrent, whatever produced it.

    v1.29: a torrent that does not come from qBittorrent needs ONE recent result to be
    displayed and evaluated: a scrape stored under any rule (also the one it had
    before switching to Forced), the import probe, or a JOAL/presence announce.
    Returns an observation dict, or None when nothing recent exists."""
    best = None
    for o in observations.values():
        if (type(o.get('leechers')) is int and o.get('sampledAt') and o.get('status') in ('récent', 'partiel')
                and o.get('successCount', 0) > 0):
            when = dt.datetime.fromisoformat(o['sampledAt']).timestamp()
            if best is None or when > best[0]:
                best = (when, {'leechers': o['leechers'], 'seeders': o.get('seeders'), 'sampledAt': o['sampledAt'],
                               'status': 'récent', 'successCount': 1, 'source': o.get('source') or 'Scrape'})
    peers = saved_peers.get(h, {})
    last, seed = peers.get('leechers', {}), peers.get('seeders', {})
    if type(last.get('count')) is int and cutoff <= last.get('time', 0) <= now and (best is None or last['time'] > best[0]):
        best = (last['time'], {'leechers': last['count'],
                               'seeders': seed.get('count') if seed.get('time') == last['time'] else None,
                               'sampledAt': dt.datetime.fromtimestamp(last['time'], dt.timezone.utc).isoformat(),
                               'status': 'récent', 'successCount': 1, 'source': last.get('source') or 'JOAL'})
    return best[1] if best else None


def latest_torrent_seeders(h, observations, saved_peers, cutoff, now):
    """Freshest seeder count of a standalone torrent (display only)."""
    best = None
    for o in observations.values():
        if type(o.get('seeders')) is int and o.get('sampledAt'):
            when = dt.datetime.fromisoformat(o['sampledAt']).timestamp()
            if best is None or when > best[0]:
                best = (when, o['seeders'], o.get('source') or 'Scrape')
    seed = saved_peers.get(h, {}).get('seeders', {})
    if type(seed.get('count')) is int and cutoff <= seed.get('time', 0) <= now and (best is None or seed['time'] > best[0]):
        best = (seed['time'], seed['count'], seed.get('source') or 'JOAL')
    return best


def solo_evaluate(client=None):
    """Standalone library: never starts, stops or imports qBittorrent torrents.

    A fresh tracker scrape (not qBittorrent's cached counters) governs JOAL
    eligibility. Losing that evidence stops the standalone session. The
    ordinary `dryRun` switch remains the mandatory opt-in for JOAL announces.
    """
    if SOLO_LIBRARY is None or SWARM_MONITOR is None:
        return
    config = read_config()
    config = simulation_config(config)
    with STATE_LOCK:
        revision = SOLO_REVISION
    library = SOLO_LIBRARY.list(config['rules'])
    records = SWARM_MONITOR.snapshot(config['swarm']['maxAgeMinutes'] * 60, limit=None)
    by_hash = {r['infoHash']: r for r in records['items']}
    saved_peers = SWARM_MONITOR.display_peers(info['hash'] for info in library)
    candidates, rows = [], []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for info in library:
        h = info['hash']
        SWARM_MONITOR.register_solo(h, info['name'], info['urls'], info.get('sizeBytes'))
        manual_rule = config.get('torrentRuleOverrides', {}).get(h)
        all_options = matching_rules(info, config['rules'], manual_rule)
        options = [(r, u) for r, u in all_options if rule_active_now(r)]
        off_schedule = [r for r, _ in all_options if not rule_active_now(r)]
        evaluations = []
        torrent_cutoff = SWARM_MONITOR.now() - config['swarm']['maxAgeMinutes'] * 60
        latest = latest_torrent_count(h, by_hash.get(h, {}).get('rules', {}), saved_peers,
                                      torrent_cutoff, SWARM_MONITOR.now())
        for candidate_rule, candidate_urls in options:
            candidate_id = candidate_rule['id']
            candidate_threshold = candidate_rule['minLeechersByHash'].get(h, candidate_rule['minLeechers'])
            observation = by_hash.get(h, {}).get('rules', {}).get(candidate_id, {})
            with SWARM_MONITOR.lock:
                history = list(SWARM_MONITOR.entries.get(h, {}).get('history', {}).get(candidate_id, []))
            # A failed request does not invalidate an earlier, still-current response.
            # Keep evidence scoped to this rule. JOAL does not identify the responding
            # tracker, so use its count only when the rule covers all torrent URLs.
            cutoff = SWARM_MONITOR.now() - config['swarm']['maxAgeMinutes'] * 60
            history = [point for point in history if cutoff <= point['time'] <= SWARM_MONITOR.now()
                       and type(point.get('leechers')) is int and point.get('status') in ('ok', 'partial')]
            evidence = []
            if observation.get('leechers') is None and history:
                evidence.append((history[-1]['time'], history[-1], 'Scrape'))
            peers = saved_peers.get(h, {})
            last = peers.get('leechers', {})
            if (last.get('source') == 'JOAL' and set(candidate_urls) == set(info['urls'])
                    and cutoff <= last['time'] <= SWARM_MONITOR.now()):
                seed = peers.get('seeders', {})
                point = {'time': last['time'], 'leechers': last['count'], 'status': 'ok',
                         'seeders': seed.get('count') if seed.get('time') == last['time'] else None}
                evidence.append((last['time'], point, 'JOAL'))
            if evidence:
                timestamp, point, source = max(evidence, key=lambda candidate: candidate[0])
                existing = observation.get('sampledAt')
                if not existing or timestamp > dt.datetime.fromisoformat(existing).timestamp():
                    observation = dict(observation, leechers=point['leechers'], seeders=point.get('seeders'),
                                       sampledAt=dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(),
                                       status='récent', successCount=1, source=source)
            # One recent result is enough, whatever rule or source produced it (v1.29), as long as
            # this rule covers every tracker of the torrent (v1.29.4): a rule limited to some
            # trackers keeps using the readings made for its own trackers.
            if latest is not None and set(candidate_urls) == set(info['urls']):
                existing = observation.get('sampledAt')
                if (type(observation.get('leechers')) is not int or not existing
                        or dt.datetime.fromisoformat(latest['sampledAt']) > dt.datetime.fromisoformat(existing)):
                    observation = dict(observation, **latest)
            passes = bool(candidate_threshold == 0 or
                          (config['swarm']['enabled'] and confirmed_leechers(
                              observation, candidate_threshold)))
            evaluations.append((passes, candidate_rule, candidate_urls, candidate_threshold, observation))
        # Prefer a rule with validated evidence, then the least restrictive
        # matching rule. Only one JOAL session is possible for this infohash.
        evaluations.sort(key=lambda x: (not x[0], rule_preference(x[1], h)))
        confirmed, rule, urls, threshold, ob = evaluations[0] if evaluations else (False, None, [], None, dict(latest or {}))
        rid = rule['id'] if rule else None
        count = ob.get('leechers')
        seed_count = ob.get('seeders')
        if seed_count is None:
            seeds = latest_torrent_seeders(h, by_hash.get(h, {}).get('rules', {}), saved_peers,
                                           torrent_cutoff, SWARM_MONITOR.now())
            if seeds is not None:
                seed_count = seeds[1]
                ob = dict(ob, seeders=seeds[1], source=ob.get('source') or seeds[2])
        mode = config.get('simulationModes', {}).get(h, 'auto')
        if mode == 'stop' or (mode == 'forced' and rid not in FORCED_IDS):
            confirmed = False
            reason = 'Stop manuel' if mode == 'stop' else 'Forced en attente de profil JOAL'
        elif not rule and off_schedule:
            reason = 'Hors plage horaire (' + schedule_label(off_schedule[0]) + ')'
        elif not rule:
            reason = (f'Règle manuelle « {manual_rule} » absente ou désactivée'
                      if manual_rule else 'Aucune règle active ne couvre les trackers')
        elif threshold == 0:
            reason = 'Contrôle des leechers désactivé (minimum 0) · relevé informatif uniquement'
        elif not config['swarm']['enabled']:
            reason = 'Relevés autonomes désactivés dans les paramètres'
        elif count is None and SWARM_MONITOR.refresh_pending(h):
            reason = 'Relevé des trackers en cours…'
        elif count is None:
            last = saved_peers.get(h, {}).get('leechers')
            if last and last['time'] < SWARM_MONITOR.now() - config['swarm']['maxAgeMinutes'] * 60:
                reason = f"Dernier relevé trop ancien (validité {config['swarm']['maxAgeMinutes']} min)"
            elif last:
                reason = 'Dernier compteur sans relevé exploitable pour les trackers de cette règle'
            elif ob.get('attemptedAt'):
                reason = 'Leechers inconnus · consulter le diagnostic trackers'
            else:
                reason = 'Leechers inconnus · premier relevé de la règle en attente'
        elif count < threshold:
            reason = f'Leechers insuffisants : {count} / minimum {threshold}'
        elif not confirmed:
            reason = 'Relevé inexploitable pour cette règle'
        else:
            reason = 'Seuil de leechers confirmé'
        observations = by_hash.get(h, {}).get('rules', {})
        display_sources = [observations[key] for key in [*(rule['id'] for rule, _ in options), ':import']
                           if key in observations]
        display_ob = max(display_sources, key=lambda item: item.get('attemptedAt') or '', default={})
        diagnostics = {}
        for observation in sorted(display_sources, key=lambda item: item.get('attemptedAt') or ''):
            for detail in observation.get('trackerDiagnostics', []):
                diagnostics[detail['domain']] = detail
        check_status = display_ob.get('status')
        if any(item.get('status') in ('partiel', 'récent') for item in display_sources):
            check_status = 'partiel' if any(item.get('state') != 'ok' for item in diagnostics.values()) else 'récent'
        row = {'peerAttempt': display_ob.get('attemptedAt'), 'peerCheckStatus': check_status,
               'infoHash': h, 'hash': h[:12], 'name': info['name'], 'fileName': info.get('fileName'),
               'trackers': info['domains'], 'sizeBytes': info.get('sizeBytes'), 'rule': rid, 'leechers': count,
               'seeders': seed_count, 'seederSource': ob.get('source') if seed_count is not None else None,
               'ruleOverride': manual_rule, 'ruleAssignment': 'manuel' if manual_rule else 'automatique',
               'source': ob.get('source') if count is not None else None,
               'sampledAt': ob.get('sampledAt'), 'threshold': threshold,
               'trackerDiagnostics': list(diagnostics.values()),
               'reason': reason, 'selected': False, 'active': False,
               'mode': 'JOAL uniquement', 'addedOn': info['addedOn']}
        rows.append(row)
        if confirmed:
            try:
                filtered = filter_torrent(info['payload'], urls, h)
                candidates.append((count if type(count) is int else -1, h, rule, filtered, row))
            except ValueError:
                row['reason'] = 'Métadonnées ou trackers invalides'
    blocked = set()
    if library and QBIT_URL:
        if client is None:
            client = QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS)
        qbit_counts = {}
        inventory = qbit_inventory_hashes(client, qbit_counts)
        for row in rows:
            h = row['infoHash']
            if inventory is None or h in inventory:
                blocked.add(h)
                row['importBlocked'] = True
                if inventory is not None and qbit_counts.get(h):
                    row['qbitCachedPeers'] = qbit_counts[h]
                row['reason'] = ('Inventaire qBittorrent indisponible : simulation importée suspendue'
                                 if inventory is None else 'Conflit : torrent présent dans qBittorrent (prioritaire)')
        candidates = [item for item in candidates if item[1] not in blocked]
    # Priority for the real qBittorrent sessions already running.
    with RUNNER_LOCK:
        qbit_active = set().union(*(ACTIVE_HASHES.get(k, set()) for k, p in RUNNERS.items()
                                    if not k.startswith('solo-') and p.poll() is None))
    available = max(0, config['maxCandidates'] - len(qbit_active))
    chosen = {}
    for _, h, rule, payload, row in sorted(candidates, key=lambda x: (config.get("simulationModes", {}).get(x[1]) != "forced", -x[0], x[1])):
        rid = 'solo-' + rule['id']
        if len(chosen) >= available:
            row['reason'] = 'Limite globale de sessions atteinte (qBittorrent prioritaire)'
            continue
        own = sum(1 for key in chosen.values() if key[0]['id'] == rule['id'])
        if own >= rule['simultaneousSeed']:
            row['reason'] = 'Limite de sessions de la règle atteinte'
            continue
        chosen[h] = (rule, payload, row)
        row['selected'] = True
    grouped = {}
    for h, (rule, payload, row) in chosen.items():
        grouped.setdefault('solo-' + rule['id'], {'rule': rule, 'payloads': {}})['payloads'][h] = payload
    with RUNNER_LOCK:
        if revision != SOLO_REVISION:
            SOLO_NOW.set()
            return
        regrouped = set()
        for rid in list(RUNNERS):
            if not rid.startswith('solo-'):
                continue
            desired = grouped.get(rid, {}).get('payloads', {})
            rule = grouped.get(rid, {}).get('rule', {})
            switched = bool(desired) and (rule.get('maxUploadRate') == 0) != (getattr(RUNNERS[rid], 'presence_only', False) is True)
            if switched or (desired and ACTIVE_HASHES.get(rid, set()) & blocked):
                regrouped.add(rid)
            if switched or config['dryRun'] or not desired or ACTIVE_HASHES.get(rid, set()) != set(desired):
                if desired and RUNNERS[rid].poll() is None and set(desired) - ACTIVE_HASHES.get(rid, set()):
                    regrouped.add(rid)
                runner_stop(rid)
        if not config['dryRun'] and not simulations_paused():
            for rid, obj in grouped.items():
                if rid in RUNNERS and RUNNERS[rid].poll() is None:
                    continue
                # Avoid restarting the same JOAL rule every few seconds.
                if rid not in regrouped and time.monotonic() - LAST_START.get(rid, -999999) < 900:
                    left = max(1, round((900 - (time.monotonic() - LAST_START.get(rid, 0))) / 60))
                    failure = runner_failure_text(rid)
                    for row in rows:
                        if row['infoHash'] in obj['payloads']:
                            row['reason'] = (f'{failure} · nouvelle tentative dans {left} min' if failure
                                             else 'Pause anti-relance (15 min)')
                    continue
                try:
                    solo_rule = dict(obj['rule'], id=rid)
                    runner_start(solo_rule, obj['payloads'])
                except Exception as exc:
                    LOG.warning('JOAL uniquement %s : lancement impossible (%s)', rid, type(exc).__name__)
                    for row in rows:
                        if row['infoHash'] in obj['payloads']:
                            row['reason'] = 'Échec lancement JOAL (vérifier logs et profil client)'
        live = set().union(*(ACTIVE_HASHES.get(rid, set()) for rid, proc in RUNNERS.items()
                             if rid.startswith('solo-') and proc.poll() is None))
    for row in rows:
        row['active'] = row['infoHash'] in live
        if row['active']:
            row['reason'] = 'Session JOAL active (aucun fichier téléchargé requis)'
            row['joalSession'] = live_joal_sessions().get(row['infoHash'], {'active': True, 'simulatedBytes': None})
        elif row['selected'] and config['dryRun']:
            row['reason'] = 'Éligible, mode dryRun : aucune annonce envoyée'
        elif row['selected']:
            row['selected'] = False
    with STATE_LOCK:
        if revision != SOLO_REVISION:
            SOLO_NOW.set()
            return
        STATUS['solo'] = {'count': len(rows), 'active': len(live), 'updatedAt': now,
                          'items': sorted(rows, key=lambda r: (not r['active'], r['name'].casefold()))[:1500]}


# ---- v1.29.4: sessions that run without measuring anything ------------------------------
STALL_AFTER_SECONDS = 120       # no fresh upload.json this long after a start: session stopped
RUNNER_FAILURES = {}            # rule id -> {'time': epoch, 'reason': fixed public label}
_LOG_REASONS = (
    ('OutOfMemoryError', 'mémoire insuffisante (OutOfMemoryError)'),
    ('BindException', 'port réseau déjà utilisé'),
    ('Address already in use', 'port réseau déjà utilisé'),
    ('NoSuchFileException', 'fichier introuvable (profil .client ou .torrent)'),
    ('JsonParseException', 'fichier de configuration ou profil illisible'),
    ('InvalidTorrent', '.torrent invalide'),
    ('Exception', 'erreur au démarrage de JOAL (voir joal.log)'),
)


def joal_log_reason(rid):
    """Short fixed label from the end of a session's joal.log (never tracker URLs or passkeys)."""
    try:
        with (DATA / 'runtime' / rid / 'joal.log').open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            tail = stream.read().decode('utf-8', 'replace')
    except OSError:
        tail = ''
    for marker, label in _LOG_REASONS:
        if marker in tail:
            return label
    return 'aucune mesure reçue de JOAL'


def runner_failure_text(rid):
    failure = RUNNER_FAILURES.get(rid)
    return f"JOAL arrêté : {failure['reason']}" if failure else None


def check_runner_health():
    """Stop a session alive without fresh measurements, record why a session exited by itself.

    JOAL keeps its JVM alive when its seeding fails to start (e.g. out of memory before
    v1.29.3): the session looked active with an "Indisponible" upload. The usual 15-minute
    anti-restart pause then applies, and the reason is shown on the torrent rows."""
    now = time.monotonic()
    with RUNNER_LOCK:
        for rid, proc in list(RUNNERS.items()):
            if proc is None:
                continue
            key = ('joal-session-' + rid)[:64]
            code = proc.poll()
            if code is not None:
                if code not in (0, -15, 143) and not getattr(proc, 'failure_noted', False):
                    proc.failure_noted = True
                    reason = (joal_log_reason(rid) if getattr(proc, 'engine', 'joal') == 'joal'
                              else 'script de présence interrompu')
                    RUNNER_FAILURES[rid] = {'time': time.time(), 'reason': reason}
                    LOG.warning('Session JOAL %s arrêtée d’elle-même (code %s) : %s', rid, code, reason)
                    notify_incident(key, True, f'Session JOAL « {rid} » arrêtée : {reason}',
                                    f'Session JOAL « {rid} » rétablie')
                continue
            started = LAST_START.get(rid)
            if started is None or now - started < STALL_AFTER_SECONDS:
                continue
            try:
                age = time.time() - os.stat(Path(proc.telemetry.name) / 'upload.json').st_mtime
            except (AttributeError, OSError):
                age = None
            if age is not None and age < 30:
                if RUNNER_FAILURES.pop(rid, None) is not None:
                    notify_incident(key, False, f'Session JOAL « {rid} » arrêtée',
                                    f'Session JOAL « {rid} » rétablie')
                continue
            reason = joal_log_reason(rid)
            LOG.warning('Session JOAL %s sans mesure depuis %d s (%s) : arrêt', rid, STALL_AFTER_SECONDS, reason)
            RUNNER_FAILURES[rid] = {'time': time.time(), 'reason': reason}
            try:
                runner_stop(rid)
            except RuntimeError:
                LOG.exception('Arrêt de la session bloquée %s non confirmé', rid)
            notify_incident(key, True, f'Session JOAL « {rid} » arrêtée : {reason}',
                            f'Session JOAL « {rid} » rétablie')


def solo_loop():
    client = None
    while not STOP.is_set():
        beat("solo")
        SOLO_NOW.clear()
        try:
            check_runner_health()
        except Exception:
            LOG.exception('Contrôle des sessions JOAL indisponible')
        try:
            client = qbit_client(client) if QBIT_URL else None
            solo_evaluate(client)
        except Exception:
            LOG.exception('Surveillance JOAL uniquement interrompue')
            with RUNNER_LOCK:
                for rid in list(RUNNERS):
                    if rid.startswith('solo-'):
                        try:
                            runner_stop(rid)
                        except Exception:
                            LOG.exception('Arrêt JOAL uniquement non confirmé')
        SOLO_NOW.wait(10)


def guard_loop():
    """Fast qBittorrent inventory refresh and fail-closed runner guard.

    The fast read also runs in dry-run mode and without JOAL processes. This
    fixes the dashboard waiting refreshSeconds to notice a newly tagged or
    resumed torrent. No HTTP tracker calls are made from this loop.
    """
    client = None
    while not STOP.is_set():
        beat("guard")
        try:
            config = read_config()
            # An empty URL (connection removed in the WebUI) raises here and
            # takes the fail-closed path below: qBittorrent sessions stop.
            client = qbit_client(client)
            torrent_list = watched_torrents(client, config)
            active = {t["hash"]: t for t in torrent_list if t.get("hash")}
            if HANDOFF_LEDGER is not None and getattr(HANDOFF_LEDGER, 'corrupt', False):
                raise RuntimeError('handoff-state.json illisible : rétablir une sauvegarde avant la gestion automatique')
            # The guard may run between two slower poll cycles. Never claim
            # qBittorrent has priority while a JOAL process is still running.
            returned_by_user = set()
            if HANDOFF_LEDGER is not None:
                for h in HANDOFF_LEDGER.owned_hashes():
                    if h in active and complete_running(active[h]):
                        with RUNNER_LOCK:
                            for rid, assigned in list(ACTIVE_HASHES.items()):
                                if h in assigned:
                                    runner_stop(rid)
                        HANDOFF_LEDGER.mark_returned(h, config["handoff"]["cooldownMinutes"],
                          'Reprise qBittorrent détectée par la garde rapide')
                        returned_by_user.add(h)
            with RUNNER_LOCK:
                running = {rid: set(ACTIVE_HASHES.get(rid, set())) for rid in RUNNERS}
                stopped_rules = set()
                for rid, hashes in running.items():
                    if rid not in RUNNERS or rid.startswith('solo-'):
                        continue
                    if (config["dryRun"] or not hashes or
                            any(h not in active or not is_complete_stopped(active[h]) for h in hashes)):
                        LOG.warning("Garde qBittorrent : arrêt JOAL %s (torrent actif, absent ou état non sûr)", rid)
                        runner_stop(rid)
                        stopped_rules.add(rid)
            if stopped_rules:
                with STATE_LOCK:
                    for item in STATUS.get("rules", []):
                        if item["id"] in stopped_rules:
                            item["state"] = "suspendue (état qBittorrent non sûr)"
                    for item in STATUS.get("torrents", []):
                        if item.get("rule") in stopped_rules and item.get("selected"):
                            item["selected"] = False
                            item["eligible"] = False
                            item["reason"] = "Suspendu : qBittorrent actif ou état inconnu"
            if CLEANUP_LEDGER is not None:
                with LEDGER_LOCK:
                    before = len(CLEANUP_LEDGER.entries)
                    CLEANUP_LEDGER.forget_absent(active)
                    for torrent in torrent_list:
                        if not is_complete_stopped(torrent):
                            CLEANUP_LEDGER.entries.pop(torrent["hash"].lower(), None)
                    if len(CLEANUP_LEDGER.entries) != before:
                        CLEANUP_LEDGER.save()
            # A former automatic stop must not leave qBittorrent idle if
            # the JOAL process has disappeared. Allow a brief staging period
            # while the slower poll is preparing the selection.
            if HANDOFF_LEDGER is not None:
                with RUNNER_LOCK:
                    assigned_now = set().union(*(set(ACTIVE_HASHES.get(rid, set()))
                        for rid, proc in RUNNERS.items() if proc.poll() is None))
                orphaned = {h for h in HANDOFF_LEDGER.owned_hashes() - assigned_now
                    if time.time() - HANDOFF_LEDGER.entry(h).get('stoppedByControllerAt', time.time()) >= 30}
                if orphaned:
                    _resume_owned(client, config, orphaned,
                                  'Session JOAL absente : priorité qBittorrent')
                    POLL_NOW.set()
            notify_incident('qbit-connection', False, 'Connexion qBittorrent indisponible',
                            'Connexion qBittorrent rétablie')
            notify_incident('joal-stop', False, 'Arrêt JOAL non confirmé', 'Arrêt JOAL confirmé')
            if update_inventory_snapshot(torrent_list) or stopped_rules or returned_by_user:
                POLL_NOW.set()
        except Exception as exc:
            client = None
            if isinstance(exc, OSError):  # network down: one line, not a traceback every 5 s
                LOG.warning("Garde qBittorrent : qBittorrent injoignable (%s), arrêt préventif", exc)
            else:
                LOG.exception("Garde qBittorrent : contrôle indisponible, arrêt préventif")
            all_stopped = stop_qbit_runners()
            notify_incident('qbit-connection', True, 'Connexion qBittorrent interrompue',
                            'Connexion qBittorrent rétablie')
            if not all_stopped:
                notify_incident('joal-stop', True, 'ALERTE : arrêt d’un processus JOAL non confirmé', 'Arrêt JOAL confirmé')
            # Snapshot under RUNNER_LOCK: iterating RUNNERS while another thread
            # modifies it would raise here and kill this safety thread.
            with RUNNER_LOCK:
                active_rules = {rid for rid, proc in RUNNERS.items() if proc.poll() is None}
            with STATE_LOCK:
                STATUS["qbitConnected"] = False
                STATUS["error"] = "Surveillance rapide indisponible : " + str(exc)[:150]
                if not all_stopped:
                    STATUS["error"] += " — ALERTE : arrêt d'un processus JOAL non confirmé"
                for item in STATUS.get("rules", []):
                    item["state"] = ("ALERTE : arrêt JOAL non confirmé" if item["id"] in active_rules
                                     else "suspendue (contrôle indisponible)")
                for item in STATUS.get("torrents", []):
                    item["selected"] = False
                    item["eligible"] = False
                    item["reason"] = "État inconnu : connexion qBittorrent indisponible"
                totals = STATUS.setdefault("totals", {})
                totals.update(tagged=None, eligible=None, blocked=None, selected=0)
            POLL_NOW.set()
        try:
            interval = read_config()["guardSeconds"]
        except Exception:
            interval = 5
        STOP.wait(interval)


def poll_loop():
    client = None
    while not STOP.is_set():
        beat("poll")
        try:
            client = qbit_client(client)
            run_once(client)
            notify_incident('qbit-poll', False, 'Évaluation qBittorrent interrompue', 'Évaluation qBittorrent rétablie')
        except Exception as exc:
            stopped = stop_qbit_runners()   # independent solo sessions are not owned by qBittorrent
            if stopped and HANDOFF_LEDGER is not None:
                try:
                    _resume_owned(QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS), read_config(),
                                  HANDOFF_LEDGER.owned_hashes(), 'Erreur surveillance : priorité qBittorrent')
                except Exception:
                    LOG.exception('Impossible de restaurer qBittorrent ; réessai au prochain cycle')
            client = None
            if isinstance(exc, OSError):
                LOG.warning("Surveillance : qBittorrent injoignable (%s)", exc)
            else:
                LOG.exception("Erreur de surveillance")
            notify_incident('qbit-poll', True, 'Évaluation qBittorrent interrompue', 'Évaluation qBittorrent rétablie')
            safe_audit('event', 'qbit.monitor_error', 'Surveillance qBittorrent interrompue', level='error')
            with STATE_LOCK:
                for row in STATUS.get("torrents", []):
                    row.update(selected=False, eligible=False,
                               reason="État inconnu : contrôle des trackers indisponible")
                STATUS.update(time=dt.datetime.now(dt.timezone.utc).isoformat(),
                              error=str(exc)[:300], qbitConnected=False, rules=[], sample=[],
                              totals={"tagged": None, "selected": 0, "eligible": None,
                                      "blocked": None, "displayed": len(STATUS.get("torrents", [])),
                                      "evaluated": 0})
        try:
            interval = read_config()["refreshSeconds"]
        except Exception:
            interval = 180
        POLL_NOW.wait(interval)
        POLL_NOW.clear()



def fresh_sample():
    """An urgent scrape (import or mode change) arrived: re-evaluate both pipelines."""
    SOLO_NOW.set()
    POLL_NOW.set()


def swarm_loop():
    """Read-only tracker watcher independent of qBittorrent connections.

    Failing tracker requests never create a zero count. This thread cannot
    launch JOAL, delete torrents, change tags or bypass qBittorrent guarding.
    """
    while not STOP.is_set():
        beat("swarm")
        SWARM_NOW.clear()
        attempted = 0
        try:
            conf = read_config()
            monitor_conf = conf["swarm"]
            if SWARM_MONITOR is not None and monitor_conf["enabled"]:
                attempted = SWARM_MONITOR.refresh(conf["rules"], max_checks=20,
                                      min_age=monitor_conf["intervalMinutes"] * 60,
                                      overrides=conf.get('torrentRuleOverrides', {}), on_sample=fresh_sample,
                                      import_probe=lambda h: not conf['dryRun'] and not simulations_paused()
                                      and TORRENT_MODES.get(h, 'auto') == 'auto' and import_probe_allowed(h))
                with SWARM_MONITOR.lock:
                    if SWARM_MONITOR.pending_imports:
                        SWARM_NOW.set()
            if SWARM_MONITOR is not None:
                snapshot = SWARM_MONITOR.snapshot(monitor_conf["maxAgeMinutes"] * 60)
                with STATE_LOCK:
                    STATUS["swarm"] = snapshot
            notify_incident('tracker-monitor', False, 'Surveillance des trackers interrompue', 'Surveillance des trackers rétablie')
        except Exception:
            LOG.exception("Surveillance tracker autonome indisponible")
            notify_incident('tracker-monitor', True, 'Surveillance autonome des trackers interrompue', 'Surveillance des trackers rétablie')
        try:
            minutes = read_config()["swarm"]["intervalMinutes"]
        except Exception:
            minutes = 15
        SWARM_NOW.wait(5 if attempted == 20 else minutes * 60)


def cleanup_sweep(client, config, now=None):
    """Observe live tracker scrapes; auto-delete only with explicit opt-in.

    qBittorrent cached counts are NEVER used as evidence for deletion.
    Scrape is read-only and disabled until the user enables cleanup.
    """
    cleanup_config = config["cleanup"]
    if not cleanup_config["enabled"]:
        with STATE_LOCK:
            STATUS["cleanup"].update(enabled=False, deleteData=False, items=[], error=None)
        return
    now = time.time() if now is None else now
    client_torrents = watched_torrents(client, config)
    # Strict cap: don't flood private trackers or accidentally scrape a whole library.
    max_checks = 60
    snapshots = []
    valid_hashes = {t["hash"] for t in client_torrents if t.get("hash")}
    checked = 0
    with LEDGER_LOCK:
        CLEANUP_LEDGER.forget_absent(valid_hashes)
    for torrent in client_torrents:
        infohash = torrent["hash"]
        name = str(torrent.get("name") or "(sans nom)")[:160]
        item = {"infoHash": infohash, "hash": infohash[:12], "name": name,
                "status": "à vérifier", "leechers": None, "zeroSince": None,
                "secondsRemaining": None, "observations": 0, "source": "scrape HTTP(S)/UDP"}
        if not is_complete_stopped(torrent) or not matches_watch(torrent, config):
            item["status"] = ("Torrent actif, incomplet ou hors du filtre tag/catégorie" if config.get("qbitFilterMode") == "either" else
                              "Torrent actif, incomplet ou hors catégorie" if config.get("qbitFilterMode") == "category"
                              else "Torrent actif, incomplet ou tag absent")
            with LEDGER_LOCK:
                CLEANUP_LEDGER.observe(infohash, False, now, 0)
        elif checked >= max_checks:
            item["status"] = "Limite des vérifications par cycle atteinte"
            with LEDGER_LOCK:
                CLEANUP_LEDGER.observe(infohash, False, now, 0)
        else:
            checked += 1
            try:
                trackers = client.trackers(infohash)
                # Hash the exact authorized source set (including configuration).
                # Never persist raw announce URLs: they may contain passkeys.
                source_scope = hashlib.sha256(json.dumps({
                    **({"category": torrent.get("category")} if config.get("qbitFilterMode") == "category" else {}),
                    **({"filter": "either", "tag": config["qbitTag"], "categories": sorted(watch_categories(config))}
                       if config.get("qbitFilterMode") == "either" else {}),
                    "urls": sorted(t["url"] for t in trackers if isinstance(t.get("url"), str)),
                    "rules": [(r["id"], sorted(r["trackerDomains"])) for r in config["rules"] if r["enabled"]]
                }, sort_keys=True).encode()).hexdigest()
                leechers, count = checked_scrapes(trackers, config["rules"], infohash)
                item["leechers"] = leechers
                if leechers:
                    item["status"] = f"{leechers} leecher(s) observé(s) — compteur remis à zéro"
                    with LEDGER_LOCK:
                        CLEANUP_LEDGER.observe(infohash, False, now, 0)
                else:
                    item["status"] = f"0 leecher sur {count} tracker(s) (scrape frais)"
                    with LEDGER_LOCK:
                        entry = CLEANUP_LEDGER.observe(infohash, True, now,
                            cleanup_config["scrapeEveryMinutes"] * 60 * 2.25 + 120, source_scope)
                    item.update(zeroSince=entry["zeroSince"], observations=entry["observations"],
                                secondsRemaining=time_until(entry, cleanup_config["afterDays"], now))
                    if item["secondsRemaining"] == 0 and entry["observations"] >= 4:
                        item["status"] = "Seuil atteint — en attente de confirmation finale"
                        if cleanup_config["deleteData"] and not config["dryRun"]:
                            # Reserve the hash first: find_candidates() refuses any
                            # torrent in PENDING_DELETE, so no JOAL session can start
                            # while the slow network re-checks run without the lock.
                            PENDING_DELETE[infohash] = time.monotonic() + 300
                            try:
                                with RUNNER_LOCK:
                                    for rid in list(RUNNERS):
                                        if infohash in ACTIVE_HASHES.get(rid, set()):
                                            runner_stop(rid)
                                current_config = read_config()
                                fresh = next((t for t in watched_torrents(client, current_config)
                                              if t.get("hash") == infohash), None)
                                if not fresh or not is_complete_stopped(fresh) or not matches_watch(fresh, current_config):
                                    raise RuntimeError("Torrent relancé ou hors du filtre")
                                if (not current_config["cleanup"]["enabled"] or
                                    not current_config["cleanup"]["deleteData"] or current_config["dryRun"]):
                                    raise RuntimeError("Nettoyage désactivé entre-temps")
                                fresh_count, _ = checked_scrapes(client.trackers(infohash), current_config["rules"], infohash)
                                if fresh_count != 0:
                                    raise RuntimeError("Leechers détectés avant suppression")
                                with RUNNER_LOCK:
                                    if any(proc.poll() is None and infohash in ACTIVE_HASHES.get(rid, set())
                                           for rid, proc in RUNNERS.items()):
                                        raise RuntimeError("Session JOAL relancée entre-temps")
                                client.delete_torrent_with_data(infohash)
                                PENDING_DELETE[infohash] = time.monotonic() + 120
                                item["status"] = "Suppression demandée à qBittorrent (avec données)"
                                with LEDGER_LOCK:
                                    CLEANUP_LEDGER.observe(infohash, False, now, 0)
                                with STATE_LOCK:
                                    STATUS["cleanup"]["deleted"] += 1
                                POLL_NOW.set()
                            except Exception as exc:
                                PENDING_DELETE.pop(infohash, None)
                                item["status"] = "Suppression annulée : " + str(exc)[:100]
                        else:
                            item["status"] = "Seuil atteint — aperçu, suppression désactivée"
            except (ScrapeUnavailable, ValueError, OSError) as exc:
                # Never use qBittorrent's cached num_leeches as fallback.
                item["status"] = str(exc)[:120]
                with LEDGER_LOCK:
                    CLEANUP_LEDGER.observe(infohash, False, now, 0)
            except Exception:
                LOG.exception("Nettoyage : contrôle du torrent %s impossible", infohash[:12])
                item["status"] = "Erreur de vérification du torrent"
                with LEDGER_LOCK:
                    CLEANUP_LEDGER.observe(infohash, False, now, 0)
        snapshots.append(item)
    with LEDGER_LOCK:
        CLEANUP_LEDGER.save()
    with STATE_LOCK:
        STATUS["cleanup"].update(enabled=True, deleteData=cleanup_config["deleteData"],
                                  lastCheck=dt.datetime.now(dt.timezone.utc).isoformat(),
                                  items=snapshots[:1500], error=None,
                                  skipped=max(0, len(client_torrents)-max_checks))
        by_hash = {i["infoHash"]: i for i in snapshots}
        for row in STATUS.get("torrents", []):
            row["cleanup"] = by_hash.get(row.get("infoHash"), {})


def cleanup_loop():
    client = None
    while not STOP.is_set():
        beat("cleanup")
        try:
            conf = read_config()
            if conf["cleanup"]["enabled"] and QBIT_URL:
                client = qbit_client(client)
                cleanup_sweep(client, conf)
            else:
                if CLEANUP_LEDGER is not None:
                    with LEDGER_LOCK:
                        if CLEANUP_LEDGER.entries:
                            CLEANUP_LEDGER.entries.clear()
                            CLEANUP_LEDGER.save()
                with STATE_LOCK:
                    STATUS["cleanup"].update(enabled=False, deleteData=False, items=[], error=None)
        except Exception as exc:
            client = None
            LOG.warning("Nettoyage suspendu : %s", type(exc).__name__)
            with STATE_LOCK:
                STATUS["cleanup"].update(error="Vérification indisponible, aucune suppression autorisée",
                                          lastCheck=dt.datetime.now(dt.timezone.utc).isoformat())
        try:
            interval = read_config()["cleanup"]["scrapeEveryMinutes"] * 60
        except Exception:
            interval = 3600
        CLEANUP_NOW.wait(interval)
        CLEANUP_NOW.clear()


STATIC = Path(__file__).resolve().parent / "static"



class Handler(BaseHTTPRequestHandler):
    server_version = f"JOALController/{APP_VERSION}"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)  # Avoid indefinitely blocked HTTP workers.

    def security_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")

    def cookie_security(self):
        return '; Secure' if os.environ.get('COOKIE_SECURE', '0') == '1' else ''

    def set_session_cookie(self, token):
        self.send_header('Set-Cookie', f'{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400{self.cookie_security()}')

    def clear_session_cookie(self):
        self.send_header('Set-Cookie', f'{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{self.cookie_security()}')

    def unauthorized(self):
        # Deliberately do not return WWW-Authenticate: Basic (browser popup).
        return self.send_json({'error': 'Session expirée ou authentification requise'}, 401)

    def session(self):
        return AUTH.get(self.headers.get('Cookie', ''))

    def client_ip(self):
        """Peer address; the last X-Forwarded-For hop only from a trusted proxy."""
        peer = self.client_address[0]
        if peer in TRUSTED_PROXIES:
            hop = self.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
            try:
                return str(ipaddress.ip_address(hop))
            except ValueError:
                pass
        return peer

    def same_origin(self):
        if self.headers.get('Sec-Fetch-Site', '').lower() == 'cross-site':
            return False
        origin = self.headers.get('Origin')
        if origin:
            try:
                parsed = urlparse(origin)
            except ValueError:
                return False
            if parsed.path or parsed.query or parsed.fragment or parsed.username is not None:
                return False
            return parsed.scheme in ('http', 'https') and parsed.netloc.lower() == self.headers.get('Host', '').lower()
        return True

    def redirect(self, target):
        self.send_response(303)
        self.send_header('Location', target)
        self.security_headers()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def send_json(self, obj, status=200, session_token=None, clear_cookie=False):
        payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.security_headers()
        if session_token:
            self.set_session_cookie(session_token)
        if clear_cookie:
            self.clear_session_cookie()
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def serve_static(self, filename, content_type):
        data = (STATIC / filename).read_bytes()
        # Both HTML screens use the version served by this process, never a
        # separately hard-coded number that can become stale on upgrades.
        if filename in ('index.html', 'login.html'):
            data = data.replace(b'__APP_VERSION__', APP_VERSION.encode('ascii'))
            data = data.replace(b'__JOAL_VERSION__', JOAL_VERSION.encode('ascii'))
            data = interface_design.render(data, 'app' if filename == 'index.html' else 'login',
                                           interface_design.current(DATA))
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.security_headers()
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def login(self):
        if not self.same_origin():
            return self.send_json({'error': 'Origine interdite'}, 403)
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self.send_json({'error': 'Content-Type application/json requis'}, 415)
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.send_json({'error': 'Taille invalide'}, 400)
        if size < 2 or size > 4096:
            return self.send_json({'error': 'Taille invalide'}, 413)
        try:
            body = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            return self.send_json({'error': 'Requête invalide'}, 400)
        if not isinstance(body, dict) or set(body.keys()) != {'username', 'password'} or \
                not all(isinstance(body[k], str) for k in ('username','password')):
            return self.send_json({'error': 'Requête invalide'}, 400)
        state, token, _csrf = AUTH.create(body['username'], body['password'], self.client_ip())
        if state == 'limited':
            return self.send_json({'error': 'Trop de tentatives'}, 429)
        if state != 'ok':
            return self.send_json({'error': 'Identifiants incorrects'}, 401)
        safe_audit('event', 'auth.login', 'Connexion à l’administration')
        return self.send_json({'authenticated': True}, 200, session_token=token)

    def do_GET(self):
        path = urlparse(self.path).path
        current_session = self.session()
        if path == '/healthz':
            # Unauthenticated liveness probe for Docker HEALTHCHECK: no data exposed.
            return self.send_json({'status': 'ok', 'version': APP_VERSION})
        if path == '/api/auth/session':
            return self.send_json({'authenticated': True, 'username': 'admin',
                                   'csrfToken': current_session['csrf']}
                                  if current_session else {'authenticated': False})
        login_files = {'/assets/login.css': ('login.css', 'text/css; charset=utf-8'),
                       '/assets/login.js': ('login.js', 'text/javascript; charset=utf-8'),
                       # Public, static icon: avoids a 401/404 on every page load.
                       '/assets/favicon.svg': ('favicon.svg', 'image/svg+xml'),
                       '/favicon.ico': ('favicon.svg', 'image/svg+xml'),
                       '/assets/design-baie-login.css': ('design-baie-login.css', 'text/css; charset=utf-8')}
        # Self-hosted fonts of the « Baie » design (OFL), also used by the login page.
        for font in ('barlow-latin-400-normal', 'barlow-latin-500-normal', 'barlow-latin-600-normal',
                     'barlow-condensed-latin-600-normal', 'barlow-condensed-latin-700-normal'):
            login_files[f'/assets/fonts/{font}.woff2'] = (f'fonts/{font}.woff2', 'font/woff2')
        if path in login_files:
            return self.serve_static(*login_files[path])
        if path == '/login':
            if current_session:
                return self.redirect('/')
            return self.serve_static('login.html', 'text/html; charset=utf-8')
        if not current_session:
            return self.redirect('/login') if path == '/' else self.unauthorized()
        if path == '/api/qbit-connection':
            return self.send_json(qbit_settings.public(DATA))
        if path == '/api/interface-design':
            return self.send_json(interface_design.public(interface_design.current(DATA)))
        if path == '/api/notifications':
            return self.send_json(NOTIFICATIONS.snapshot() if NOTIFICATIONS is not None
                                  else {'items': [], 'unread': 0, 'loadWarning': True})
        if path == '/api/swarm/history':
            query = parse_qs(urlparse(self.path).query)
            hashes, rules = query.get('infoHash', []), query.get('rule', [])
            if len(hashes) != 1 or len(rules) != 1 or not HASH_RE.fullmatch(hashes[0].lower()):
                return self.send_json({'error': 'Paramètres invalides'}, 400)
            try:
                return self.send_json(SWARM_MONITOR.history(hashes[0], rules[0]))
            except (KeyError, ValueError):
                return self.send_json({'error': 'Historique non disponible'}, 404)
        if path == '/api/torrent/peers':
            hashes = parse_qs(urlparse(self.path).query).get('infoHash', [])
            if len(hashes) != 1 or not HASH_RE.fullmatch(hashes[0].lower()):
                return self.send_json({'error': 'Infohash invalide'}, 400)
            if SWARM_MONITOR is None:
                return self.send_json({'error': 'Relevés indisponibles'}, 503)
            try:
                return self.send_json(SWARM_MONITOR.peer_series(hashes[0].lower()))
            except (KeyError, ValueError):
                return self.send_json({'infoHash': hashes[0].lower(), 'points': [], 'retentionDays': 14})
        if path == '/api/backup/export':
            try:
                payload = build_backup(DATA)
            except (OSError, ValueError):
                return self.send_json({'error': 'Sauvegarde indisponible ou trop volumineuse'}, 503)
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.security_headers()
            self.send_header('Content-Disposition', 'attachment; filename="joal-smart-data-backup.zip"')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if path == "/api/health":
            return self.send_json(health_snapshot())
        if path == "/api/diagnostics":
            query = parse_qs(urlparse(self.path).query)
            hashes = query.get('infoHash', [])
            if len(hashes) > 1 or (hashes and not HASH_RE.fullmatch(hashes[0].lower())):
                return self.send_json({'error': 'Infohash invalide'}, 400)
            if DIAGNOSTICS is None:
                return self.send_json({'error': 'Journal non initialisé'}, 503)
            return self.send_json(DIAGNOSTICS.snapshot(infohash=hashes[0].lower() if hashes else None))
        if path == "/api/config":
            try:
                return self.send_json(read_config())
            except Exception as e:
                return self.send_json({"error": str(e)}, 500)
        if path == "/api/upload-history":
            query = parse_qs(urlparse(self.path).query)
            now = int(time.time())
            try:
                end = max(now - upload_history.RETENTION + 3600, min(now, int(query.get("end", [now])[0])))
                start = max(end - 3600, int(query.get("since", [end - 3600])[0]))
            except ValueError:
                return self.send_json({"error": "Période invalide"}, 400)
            with STATE_LOCK:
                recent = list(UPLOAD_HISTORY)
            try:
                old = upload_history.window((DATA / "upload-history.sqlite").resolve(), start, end) if not recent or start < recent[0][0] else []
            except sqlite3.Error:
                return self.send_json({"error": "Historique temporairement indisponible"}, 503)
            merged = dict(old)
            merged.update((t, r) for t, r in recent if start <= t <= end)
            return self.send_json({"end": end, "earliest": now - upload_history.RETENTION,
                                   "points": sorted(merged.items())})
        if path == "/api/state":
            with STATE_LOCK:
                snapshot = copy.deepcopy(STATUS)
            # Reflect newly queued manual requests without waiting for the
            # slower tracker evaluation loop to regenerate the table.
            if HANDOFF_LEDGER is not None:
                for row in snapshot.get('torrents', []):
                    if row.get('infoHash'):
                        row['handoff'] = HANDOFF_LEDGER.public_status(row['infoHash'])
            with STATE_LOCK:
                snapshot["uploadHistory"] = list(UPLOAD_HISTORY)[::5]
            snapshot["simulationModes"] = TORRENT_MODES
            snapshot["simulationsPaused"] = simulations_paused()
            snapshot["joalUpload"] = live_joal_upload()
            announce_issue_rows(snapshot)
            upload_hint_rows(snapshot)
            if SWARM_MONITOR is not None:
                rows = [*snapshot.get('torrents', []), *snapshot.get('solo', {}).get('items', [])]
                saved = SWARM_MONITOR.display_peers(row.get('infoHash') for row in rows)
                for row in rows:
                    row['lastPeers'] = saved.get(row.get('infoHash'), {})
            return self.send_json(snapshot)
        if path == "/api/clients":
            clients_path = DATA / "clients"
            names = sorted(f.name for f in clients_path.glob("*.client") if f.is_file()) if clients_path.is_dir() else []
            return self.send_json({"clients": names})
        static_files = {"/": ("index.html", "text/html; charset=utf-8"),
                        "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
                        "/assets/design-baie.css": ("design-baie.css", "text/css; charset=utf-8"),
                        "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
                        "/assets/torrent_table.js": ("torrent_table.js", "text/javascript; charset=utf-8")}
        if path in static_files:
            return self.serve_static(*static_files[path])
        return self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/auth/login':
            return self.login()
        current_session = self.session()
        if not current_session:
            return self.unauthorized()
        if not self.same_origin():
            return self.send_json({'error': 'Requête intersites interdite'}, 403)
        if not AUTH.verify_csrf(current_session, self.headers.get('X-CSRF-Token')):
            return self.send_json({'error': 'Jeton de sécurité manquant ou expiré'}, 403)
        if path == '/api/qbit-connection':
            if self.headers.get_content_type() != 'application/json':
                return self.send_json({'error': 'JSON requis'}, 415)
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 16384:
                    return self.send_json({'error': 'Taille de requête invalide'}, 413)
                qbit_settings.save(DATA, json.loads(self.rfile.read(length)))
                apply_qbit_connection()
                safe_audit('event', 'settings.qbit', 'Connexion qBittorrent modifiée et appliquée')
                return self.send_json(qbit_settings.public(DATA))
            except (ValueError, UnicodeError):
                return self.send_json({'error': 'Paramètres de connexion invalides. Vérifier l’URL HTTP(S).'}, 400)
            except OSError:
                return self.send_json({'error': 'Impossible d’enregistrer la connexion dans /data'}, 500)
        if path == '/api/interface-design':
            if self.headers.get_content_type() != 'application/json':
                return self.send_json({'error': 'JSON requis'}, 415)
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 1024:
                    return self.send_json({'error': 'Taille de requête invalide'}, 413)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {'design'}:
                    raise ValueError()
                design = interface_design.save(DATA, body['design'])
            except (ValueError, TypeError, UnicodeError):
                return self.send_json({'error': 'Design inconnu'}, 400)
            except OSError:
                return self.send_json({'error': 'Impossible d’enregistrer le design dans /data'}, 500)
            return self.send_json(interface_design.public(design))
        if path == '/api/torrent-modes':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 500000:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                hashes, mode = payload['hashes'], payload['mode']
                if mode not in ('auto', 'forced', 'stop') or not isinstance(hashes, list) or not hashes:
                    raise ValueError()
                if not all(isinstance(h, str) and HASH_RE.fullmatch(h) for h in hashes):
                    raise ValueError()
                conf = read_config() if mode == 'forced' else None
                if conf is not None and set(hashes) - idle_assignments(conf) and not any(r.get('client') for r in conf['rules']):
                    return self.send_json({'error': 'Configurer au moins un profil client JOAL.'}, 409)
                set_torrent_modes(set(hashes), mode)
            except (KeyError, ValueError, TypeError):
                return self.send_json({'error': 'Mode ou sélection invalide'}, 400)
            except (OSError, RuntimeError):
                return self.send_json({'error': 'Commande incomplète : vérifier les sessions puis réessayer.'}, 503)
            return self.send_json({'mode': mode, 'count': len(set(hashes))})
        if path in ('/api/simulations/pause', '/api/simulations/resume'):
            paused = path.endswith('/pause')
            try:
                set_simulations_paused(paused)
            except (RuntimeError, OSError):
                return self.send_json({'error': 'Commande incomplète. Vérifier l’état et réessayer.'}, 503)
            return self.send_json({'paused': paused})
        if path == '/api/auth/logout':
            AUTH.revoke(self.headers.get('Cookie', ''))
            safe_audit('event', 'auth.logout', 'Déconnexion de l’administration')
            return self.send_json({'success': True}, clear_cookie=True)
        if path == '/api/notifications/read':
            if NOTIFICATIONS is None:
                return self.send_json({'error': 'Notifications indisponibles'}, 503)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except (ValueError, TypeError):
                return self.send_json({'error': 'Longueur incorrecte'}, 400)
            if length > 256 or length < 2 or self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                return self.send_json({'error': 'JSON requis'}, 400)
            try:
                body = json.loads(self.rfile.read(length))
                if body != {'all': True}:
                    raise ValueError('action invalide')
                return self.send_json({'read': NOTIFICATIONS.acknowledge()})
            except (ValueError, TypeError):
                return self.send_json({'error': 'Action incorrecte'}, 400)
        if path == '/api/backup/restore':
            if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/zip':
                return self.send_json({'error': 'ZIP requis'}, 415)
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except (ValueError, TypeError):
                return self.send_json({'error': 'Longueur incorrecte'}, 400)
            if length <= 0 or length > MAX_ARCHIVE:
                return self.send_json({'error': 'ZIP trop volumineux'}, 413)
            try:
                if not read_config()['dryRun']:
                    return self.send_json({'error': 'Activer dryRun avant une restauration'}, 409)
                with RUNNER_LOCK:
                    if any(proc.poll() is None for proc in RUNNERS.values()):
                        return self.send_json({'error': 'Arrêter les sessions JOAL avant restauration'}, 409)
                if HANDOFF_LEDGER and HANDOFF_LEDGER.owned_hashes():
                    return self.send_json({'error': 'Rendre les torrents à qBittorrent avant restauration'}, 409)
                total = schedule_restore(DATA, self.rfile.read(length))
                safe_audit('event', 'backup.staged', 'Restauration préparée : redémarrage du conteneur requis')
                return self.send_json({'queued': True, 'files': total,
                                       'message': 'Sauvegarde validée. Redémarrer le conteneur pour appliquer la restauration. dryRun sera activé.'}, 202)
            except (ValueError, OSError) as exc:
                return self.send_json({'error': str(exc)[:180]}, 400)
        if path not in ("/api/config", "/api/import/inspect", "/api/import/add", "/api/torrents/handoff", "/api/solo/delete"):
            return self.send_json({"error": "Not found"}, 404)
        if path in ('/api/config', '/api/torrents/handoff', '/api/solo/delete') and self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            return self.send_json({'error': 'Content-Type application/json requis'}, 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return self.send_json({"error": "Content-Length invalide"}, 400)
        max_size = MAX_BATCH_MULTIPART_BYTES if path.startswith("/api/import/") else 60000
        if length < 2 or length > max_size:
            return self.send_json({"error": f"Taille de requête invalide (maximum {max_size:,} octets)"}, 413)
        if path.startswith("/api/import/"):
            return self.import_torrent(path, length)
        if path == '/api/torrents/handoff':
            return self.request_manual_handoff(length)
        if path == '/api/solo/delete':
            return self.delete_solo(length)
        try:
            conf = json.loads(self.rfile.read(length))
            validate(conf)
            missing = rules_missing_client(conf)
            if missing:
                raise ConfigError("Règle(s) " + ", ".join(missing) +
                                  " : choisir un profil client JOAL (.client) ou régler les débits à 0")
            with STATE_LOCK:
                write_config(conf)
            safe_audit('event', 'settings.saved', 'Configuration enregistrée')
            SOLO_NOW.set()
            POLL_NOW.set()
            CLEANUP_NOW.set()
            SWARM_NOW.set()
        except (ValueError, ConfigError) as exc:
            return self.send_json({"error": str(exc)}, 400)
        except OSError:
            LOG.exception("Impossible d'écrire policy.json")
            return self.send_json({"error": "Impossible d'enregistrer la configuration"}, 500)
        except Exception:
            # Never drop the connection without a JSON answer on malformed input.
            LOG.exception("Configuration refusée (structure inattendue)")
            return self.send_json({"error": "Structure de configuration invalide"}, 400)
        return self.send_json({"success": True})

    def request_manual_handoff(self, length):
        """Queue a guarded, per-torrent request. HTTP does not switch clients."""
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {'infoHash', 'target'}:
                return self.send_json({'error': 'Données de basculement incorrectes'}, 400)
            h, target = body['infoHash'], body['target']
            if not isinstance(h, str) or not HASH_RE.fullmatch(h.lower()) or target not in ('joal', 'qbit'):
                return self.send_json({'error': 'Infohash ou cible invalide'}, 400)
            h = h.lower()
            if HANDOFF_LEDGER is None or HANDOFF_LEDGER.corrupt:
                return self.send_json({'error': 'Gestion du basculement indisponible'}, 503)
            config = read_config()
            if config['dryRun']:
                return self.send_json({'error': 'Désactiver dryRun pour effectuer un basculement réel'}, 409)
            if target == 'joal' and not config['handoff']['enabled']:
                return self.send_json({'error': 'Activer le basculement automatique dans les paramètres'}, 409)
            client = QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS)
            inventory = {t['hash'].lower(): t for t in watched_torrents(client, config)}
            torrent = inventory.get(h)
            if torrent is None:
                return self.send_json({'error': 'Torrent absent ou hors du filtre de surveillance'}, 404)
            if torrent.get('progress') != 1 or torrent.get('amount_left', 0) != 0:
                return self.send_json({'error': 'Fichiers incomplets : basculement interdit'}, 409)
            if target == 'joal':
                if not (complete_running(torrent) or is_complete_stopped(torrent)):
                    return self.send_json({'error': 'État qBittorrent non sûr : basculement interdit'}, 409)
                tracker_hosts = [domain(tr.get('url', '')) for tr in client.trackers(h)]
                assigned_id = config.get('torrentRuleOverrides', {}).get(h)
                if not any(rule['enabled'] and (not assigned_id or rule['id'] == assigned_id) and
                           any(host and (assigned_id == rule['id'] or any(domain_matches(host, pat) for pat in rule['trackerDomains']))
                               for host in tracker_hosts)
                           for rule in config['rules']):
                    return self.send_json({'error': 'Aucune règle active ne correspond au tracker'}, 409)
                if HANDOFF_LEDGER.cooldown_remaining(h):
                    return self.send_json({'error': 'Priorité qBittorrent : temporisation de 60 min minimum non expirée'}, 409)
            request = HANDOFF_LEDGER.queue_manual(
                h, target, 'stopped' if is_complete_stopped(torrent) else 'running')
            safe_audit('event', 'handoff.requested',
                       'Basculement manuel demandé vers ' + ('qBittorrent' if target == 'qbit' else 'JOAL'),
                       infohash=h)
            POLL_NOW.set()
            if target == 'joal':
                SWARM_NOW.set()  # minimum tracker scrape interval still applies
            return self.send_json({'accepted': True, 'status': request['status'],
                                   'message': ('Retour qBittorrent demandé' if target == 'qbit'
                                               else 'Demande JOAL prioritaire enregistrée : contrôles obligatoires maintenus')}, 202)
        except (ValueError, TypeError):
            return self.send_json({'error': 'Corps JSON incorrect'}, 400)
        except Exception as exc:
            LOG.warning('Demande de basculement refusée (%s)', type(exc).__name__)
            return self.send_json({'error': 'État qBittorrent non vérifiable : demande refusée'}, 503)

    def delete_solo(self, length):
        """Delete only the private .torrent metadata, never qBittorrent data.

        {"infoHash": h} removes one file (404 when absent); {"infoHashes": [...]} (v1.30.2,
        up to 500) removes the selection in one pass and reports the hashes not found."""
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) not in ({'infoHash'}, {'infoHashes'}):
                raise ValueError('Corps invalide')
            single = 'infoHash' in body
            raw = [body['infoHash']] if single else body['infoHashes']
            if not isinstance(raw, list) or not 1 <= len(raw) <= 500:
                raise ValueError('Liste invalide')
            hashes = list(dict.fromkeys(str(h).lower() for h in raw))
            if SOLO_LIBRARY is None or not all(HASH_RE.fullmatch(h) for h in hashes):
                raise ValueError('Infohash invalide')
            wanted = set(hashes)
            with SOLO_EDIT_LOCK, RUNNER_LOCK:
                for rid, active in list(ACTIVE_HASHES.items()):
                    if rid.startswith('solo-') and active & wanted:
                        runner_stop(rid)
                removed = SOLO_LIBRARY.delete_many(hashes)
                for h in hashes:
                    if h in removed:
                        publish_solo_edit(h)
                        if SWARM_MONITOR is not None:
                            SWARM_MONITOR.forget_solo(h)
            if single and not removed:
                raise FileNotFoundError(hashes[0])
            forget_torrent_settings(removed)
            for h in hashes:
                if h in removed:
                    safe_audit('event', 'solo.removed', 'Fichier .torrent JOAL uniquement retiré', infohash=h)
            SWARM_NOW.set()
            if single:
                return self.send_json({'success': True, 'infoHash': hashes[0]})
            return self.send_json({'success': True, 'removed': [h for h in hashes if h in removed],
                                   'missing': [h for h in hashes if h not in removed]})
        except (ValueError, TypeError, ImportErrorValue):
            return self.send_json({'error': 'Demande de retrait invalide'}, 400)
        except FileNotFoundError:
            return self.send_json({'error': 'Torrent introuvable dans la bibliothèque JOAL'}, 404)
        except (OSError, RuntimeError):
            return self.send_json({'error': 'Arrêt du processus non confirmé ou fichier non supprimable'}, 503)

    def import_torrent(self, path, length):
        """Preview or import up to 30 independent .torrent files in a single call."""
        try:
            files = parse_upload_batch(self.headers.get('Content-Type', ''), self.rfile.read(length))
            conf = read_config()
        except (ImportErrorValue, ValueError) as exc:
            return self.send_json({'error': str(exc)}, 400)
        if path == '/api/import/add' and SOLO_LIBRARY is None:
            return self.send_json({'error': 'Bibliothèque JOAL non initialisée'}, 503)
        results = []
        for row in files:
            filename = row['filename']
            if row['error']:
                results.append({'filename': filename, 'status': 'error', 'message': row['error']})
                continue
            payload = row['payload']
            try:
                if path == '/api/import/inspect':
                    info = inspect_torrent(payload, conf['rules'], require_match=False)
                else:
                    info, pending = add_to_library(payload, filename, conf)
                public = {'filename': filename, 'name': info['name'], 'hash': info['hash'],
                          'domains': info['domains'], 'matchedDomains': info['matchedDomains']}
                if path == '/api/import/inspect':
                    results.append(dict(public, status='ok'))
                    continue
                results.append(dict(public, status='added', item=pending, storedFileName=info['fileName'], message='Enregistré dans /data/torrents'))
            except ImportErrorValue as exc:
                duplicate = 'déjà dans la bibliothèque' in str(exc)
                results.append({'filename': filename, 'status': 'duplicate' if duplicate else 'error',
                                'message': str(exc)})
            except (OSError, ValueError) as exc:
                LOG.warning('Échec import JOAL uniquement (%s)', type(exc).__name__)
                results.append({'filename': filename, 'status': 'error',
                                'message': 'Écriture refusée dans /data/torrents : vérifier les droits du dossier (nobody:users)'})
        added = sum(row['status'] == 'added' for row in results)
        if added:
            SOLO_NOW.set()
            SWARM_NOW.set()
            POLL_NOW.set()
        if len(results) == 1:
            row = results[0]
            if row['status'] == 'ok':
                return self.send_json({'success': True, 'torrent': {
                    k: row[k] for k in ('name', 'hash', 'domains', 'matchedDomains')}})
            if row['status'] == 'added':
                return self.send_json({'success': True, 'message': 'Fichier .torrent enregistré dans /data/torrents (JOAL uniquement, aucune importation qBittorrent)',
                                       'name': row['name'], 'hash': row['hash'],
                                       'mode': 'solo', 'item': row['item'], 'matchedDomains': row['matchedDomains'],
                                       'storedFileName': row['storedFileName']})
            return self.send_json({'error': row['message']}, 409 if row['status']=='duplicate' else 400)
        return self.send_json({'success': True, 'results': results, 'added': added,
                               'duplicates': sum(x['status']=='duplicate' for x in results),
                               'failed': sum(x['status']=='error' for x in results)})



def request_shutdown(*_args):
    """Signal handler: only set flags. The blocking work runs in main()."""
    STOP.set()
    POLL_NOW.set()
    CLEANUP_NOW.set()
    SWARM_NOW.set()
    SOLO_NOW.set()


def shutdown(*_args):
    if SHUTDOWN_DONE.is_set():
        return
    SHUTDOWN_DONE.set()
    request_shutdown()
    if stop_all() and HANDOFF_LEDGER is not None and HANDOFF_LEDGER.owned_hashes():
        # Best effort: always give real sharing priority if the controller
        # is intentionally stopped. If the API is down, the persisted ledger
        # will retry restoration at the next start.
        try:
            _resume_owned(QBitClient(QBIT_URL, QBIT_USER, QBIT_PASS), read_config(),
                          HANDOFF_LEDGER.owned_hashes(), 'Arrêt du contrôleur : priorité qBittorrent')
        except Exception:
            LOG.exception('Retour qBittorrent différé : API indisponible')


def apply_restore_safely():
    """Apply a pending restore; set a rejected archive aside instead of crashing.

    Returns None on success (or nothing pending), else a short reason. Raising
    here would make Docker restart the container in an endless loop.
    """
    try:
        if apply_pending_restore(DATA):
            LOG.warning('Restauration /data appliquée : dryRun activé, vérifier la configuration')
        return None
    except (ValueError, OSError) as exc:
        reason = str(exc)[:120] or type(exc).__name__
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')
        try:
            (DATA / PENDING_NAME).replace(DATA / f'.restore-rejected-{stamp}.zip')
        except OSError:
            LOG.exception('Archive de restauration impossible à mettre de côté')
        LOG.error('Restauration refusée (%s) : configuration actuelle conservée', reason)
        return reason


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not PASSWORD or PASSWORD.startswith("REMPLACER") or len(PASSWORD) < 12:
        raise SystemExit("Définir ADMIN_PASSWORD (12 caractères minimum) dans .env")
    DATA.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        shutil.copy("/app/config.example.json", CONFIG)
    ensure_config_permissions()
    # Apply a previously validated restore before any watcher or JOAL thread starts.
    restore_rejected = apply_restore_safely()
    read_config()
    global TORRENT_MODES
    mode_file = DATA / 'simulation-modes.json'
    if mode_file.exists():
        try:
            modes = json.loads(mode_file.read_text(encoding='utf-8'))
            if not isinstance(modes, dict) or not all(isinstance(h, str) and HASH_RE.fullmatch(h)
                                                      and mode in ('forced', 'forced-zero', 'stop') for h, mode in modes.items()):
                raise ValueError('contenu invalide')
            # 'forced-zero' (v1.25.4) is read back as a plain Forced.
            TORRENT_MODES = {h: "forced" if mode == "forced-zero" else mode for h, mode in modes.items()}
        except (ValueError, OSError):
            # Fail safe: every torrent falls back to Auto (no Forced session).
            mode_file.replace(mode_file.with_name('simulation-modes.invalid.json'))
            LOG.error('simulation-modes.json invalide : mis de côté, tous les torrents repassent en Auto')
    global NOTIFICATIONS
    NOTIFICATIONS = NotificationCenter(DATA / 'notifications.json')
    global CLEANUP_LEDGER
    CLEANUP_LEDGER = Ledger(DATA / "cleanup-observations.json")
    global SWARM_MONITOR
    SWARM_MONITOR = SwarmMonitor(DATA / "tracker-registry.json")
    SWARM_MONITOR.identity_for = probe_identity
    global SOLO_LIBRARY
    SOLO_LIBRARY = SoloLibrary(DATA / 'torrents')
    try:
        SOLO_LIBRARY.normalize_permissions()
    except (OSError, ImportErrorValue) as exc:
        LOG.warning('Permissions bibliothèque .torrent non normalisées (%s); imports à vérifier', type(exc).__name__)
    for meta in SOLO_LIBRARY.list(read_config()['rules']):
        SWARM_MONITOR.register_solo(meta['hash'], meta['name'], meta['urls'], meta.get('sizeBytes'))
    global HANDOFF_LEDGER
    HANDOFF_LEDGER = HandoffLedger(DATA / "handoff-state.json")
    global DIAGNOSTICS
    try:
        DIAGNOSTICS = DiagnosticsStore(DATA / "diagnostics.json")
    except OSError:
        LOG.warning('Journal de diagnostic inaccessible : surveillance maintenue sans historique')
    with STATE_LOCK:
        STATUS["swarm"] = SWARM_MONITOR.snapshot(read_config()["swarm"]["maxAgeMinutes"] * 60)
    # Startup: scrape right away every torrent whose last sample is too old to be
    # used, instead of waiting for the regular rotation. Stopped torrents and
    # torrents with a usable sample are skipped to spare the trackers.
    stale = SWARM_MONITOR.stale_hashes(read_config()["swarm"]["maxAgeMinutes"] * 60)
    stale -= {h for h, mode in TORRENT_MODES.items() if mode == 'stop'}
    if SWARM_MONITOR.request_refresh(stale):
        LOG.info("Démarrage : relevé immédiat de %d torrent(s) au relevé trop ancien", len(stale))
        SWARM_NOW.set()
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    if restore_rejected:
        notify_incident('backup-restore', True, 'Restauration refusée : ' + restore_rejected,
                        'Restauration appliquée')
    apply_qbit_connection()
    history_worker = supervised(upload_history_loop, "upload-history")
    history_worker.start()
    supervised(swarm_loop, "swarm").start()
    supervised(solo_loop, "solo", on_crash=stop_solo_runners).start()
    if os.environ.get('IMPORT_WATCH', '1') != '0':
        supervised(import_watch_loop, "import-watch").start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.timeout = 1
    LOG.info("JOAL Smart Controller v%s : interface disponible sur le port %d", APP_VERSION, PORT)
    try:
        while not STOP.is_set():
            server.handle_request()
    finally:
        shutdown()
        history_worker.join(timeout=4)
        server.server_close()


if __name__ == "__main__":
    main()
