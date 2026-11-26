"""End-to-end check on the test bench, with the real JOAL engine.

Not part of the unit suite (needs Java and a JOAL jar). Run:
  JOAL_JAR=/path/joal.jar JOAL_CLIENTS=/path/clients python3 tests/e2e_testbed.py

Scenario:
 1. local test tracker that only accepts qBittorrent peer_ids (-qB);
 2. a .torrent dropped into the watched folder (data/import/);
 3. a zero-rate rule with a qBittorrent .client profile, dryRun off;
 4. expected: import, JOAL announce accepted as a seeder (left=0), counters
    and chart points visible, healthy threads, rotated log file present;
 5. second run without any .client profile: the fallback script's announce
    is refused and the row shows "Annonce refusée : Client refusé…";
 6. Stop → Auto triggers an immediate scrape and the session starts;
 7. imported torrent matched to the 0/0 rule by tracker, set to Forced, with a
    leecher and another seeder: a real upload (> 0) is announced.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from webtest_helpers import login, auth_headers  # noqa: E402

PASSWORD = "testbed-password-123"
JAR = os.environ.get("JOAL_JAR")
CLIENTS = os.environ.get("JOAL_CLIENTS")
CLIENT = os.environ.get("JOAL_CLIENT", "qbittorrent-5.1.4.client")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as res:
        return json.load(res)


def wait(label, check, timeout=150):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = check()
            if last:
                print(f"  ✓ {label}")
                return last
        except Exception as exc:  # retried until timeout
            last = exc
        time.sleep(2)
    raise AssertionError(f"Échec : {label} (dernier état : {last!r})")


def fake_leecher(tracker_port, infohash, seeder=False):
    """Register one leecher (left > 0), or another seeder, on the test tracker."""
    import urllib.parse
    peer_id = b'-qB5140-seeder000001' if seeder else b'-qB5140-leecher00001'
    query = urllib.parse.urlencode({'info_hash': bytes.fromhex(infohash), 'peer_id': peer_id,
                                    'port': 51414 if seeder else 51413, 'uploaded': 0, 'downloaded': 0,
                                    'left': 0 if seeder else 1000, 'event': 'started'})
    urllib.request.urlopen(f"http://127.0.0.1:{tracker_port}/announce?{query}", timeout=5).read()


def scenario(with_profile, mode_switch=False, forced=False):
    work = Path(tempfile.mkdtemp(prefix="joal-e2e-"))
    tracker_port, ui_port = free_port(), free_port()
    data = work / "data"
    (data / "import").mkdir(parents=True)
    env_base = dict(os.environ, PYTHONUNBUFFERED="1")
    tracker = subprocess.Popen([sys.executable, str(ROOT / "testbed" / "tracker.py")],
                               env=dict(env_base, TRACKER_PORT=str(tracker_port), TRACKER_INTERVAL="30",
                                        ALLOWED_CLIENTS="-qB"), stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, str(ROOT / "testbed" / "make_torrents.py")], check=True,
                   env=dict(env_base, ANNOUNCE=f"http://127.0.0.1:{tracker_port}/announce", COUNT="1",
                            OUT_DIR=str(work / "generated")), stdout=subprocess.DEVNULL)
    torrent = next((work / "generated" / "torrents").glob("*.torrent"))
    rule = {"id": "seed-sans-upload", "enabled": True, "trackerDomains": ["127.0.0.1"],
            "minLeechers": 1 if mode_switch else 0,
            "minUploadRate": 0, "maxUploadRate": 0, "simultaneousSeed": 5,
            "client": CLIENT if with_profile else None}
    policy = json.loads((ROOT / "config.example.json").read_text())
    rules = [rule]
    if forced:
        # Imported torrent matches the 0/0 rule by tracker only; Forced must use
        # the rates of the first enabled rule with a client (v1.25.6 fix).
        rules = [dict(rule, id="upload", trackerDomains=["autre.example"], minUploadRate=40,
                      maxUploadRate=60, client=CLIENT), rule]
    policy.update(dryRun=False, rules=rules)
    (data / "policy.json").write_text(json.dumps(policy))
    if with_profile:
        shutil.copytree(CLIENTS, data / "clients")
    controller = subprocess.Popen([sys.executable, "-u", "main.py"], cwd=ROOT / "app",
                                  env=dict(env_base, DATA_DIR=str(data), ADMIN_PASSWORD=PASSWORD,
                                           PORT=str(ui_port), JOAL_JAR=JAR, ALLOW_PRIVATE_TRACKERS="1",
                                           QBIT_URL="", TORRENT_UID=str(os.getuid()), TORRENT_GID=str(os.getgid())),
                                  stdout=open(work / "controller.log", "wb"), stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{ui_port}"
    try:
        wait("contrôleur démarré", lambda: get(base + "/healthz")["status"] == "ok", 30)
        headers = auth_headers(*login(base, PASSWORD))
        shutil.copy(torrent, data / "import" / torrent.name)
        os.utime(data / "import" / torrent.name, (time.time() - 60, time.time() - 60))
        wait("import par le dossier surveillé", lambda: (data / "import" / "traités" / torrent.name).exists(), 60)

        def solo_row():
            items = get(base + "/api/state", headers)["solo"]["items"]
            return items[0] if items else None
        if forced:
            row = wait("torrent importé visible", solo_row)
            h = row["infoHash"]
            urllib.request.urlopen(urllib.request.Request(
                base + "/api/torrent-modes", json.dumps({"hashes": [h], "mode": "forced"}).encode(),
                headers={**headers, "Content-Type": "application/json"}, method="POST"), timeout=10).read()
            # JOAL uploads only when another seeder exists (weight 0 if seeders == 0):
            # with a lone leecher, the row must explain why the upload stays at 0.
            def hint():
                fake_leecher(tracker_port, h)
                return (solo_row() or {}).get("uploadHint")
            print("  indication :", wait("explication « aucun autre seeder » affichée",
                                         lambda: (lambda x: x if x and "autre seeder" in x else None)(hint()), 120))
            def uploading():
                fake_leecher(tracker_port, h)
                fake_leecher(tracker_port, h, seeder=True)
                stats = get(f"http://127.0.0.1:{tracker_port}/stats")
                return next((e for e in stats['recentAnnounces'] if e['infoHash'] == h
                             and e.get('left') == 0 and e['uploaded'] > 0
                             and 'seeder000001' not in str(e)), None)
            event = wait("upload simulé > 0 annoncé au tracker en Forced", uploading, 240)
            wait("explication retirée une fois l'upload lancé", lambda: not solo_row().get("uploadHint"), 60)
            print("  annonce :", {k: event[k] for k in ('client', 'uploaded', 'event') if k in event})
            mode_file = json.loads((data / "simulation-modes.json").read_text())
            assert mode_file == {h: "forced"}, mode_file
            return
        if mode_switch:
            row = wait("torrent importé visible", solo_row)
            h = row["infoHash"]
            post = lambda mode: urllib.request.urlopen(urllib.request.Request(
                base + "/api/torrent-modes", json.dumps({"hashes": [h], "mode": mode}).encode(),
                headers={**headers, "Content-Type": "application/json"}, method="POST"), timeout=10).read()
            post("stop")
            fake_leecher(tracker_port, h)
            wait("premier relevé (à l'import)", lambda: solo_row().get("sampledAt"))
            first = solo_row()["sampledAt"]
            print("  attente de 65 s (garde-fou anti-martèlement de 60 s)…")
            time.sleep(65)
            switched = time.time()
            post("auto")
            wait("nouveau relevé après passage en Auto",
                 lambda: (lambda r: r if r.get("sampledAt") and r["sampledAt"] != first else None)(solo_row()), 30)
            wait("session démarrée", lambda: solo_row().get("active"), 30)
            print(f"  démarré {time.time() - switched:.0f} s après le passage en Auto · leechers vus : {solo_row().get('leechers')}")
            return
        if with_profile:
            def seeder():
                swarms = get(f"http://127.0.0.1:{tracker_port}/stats")["swarms"]
                return next((p for s in swarms.values() for p in s["peers"]
                             if p["client"].startswith("-qB") and p["left"] == 0), None)
            print("  pair vu par le tracker :", wait("JOAL annoncé comme seeder qBittorrent", seeder))
            row = wait("annonce acceptée affichée", lambda: (lambda r: r if r and r.get("lastAnnounce", {}).get("result") == "Acceptée" else None)(solo_row()))
            # Count only later seeder announces while a leecher is registered.
            h = row["infoHash"]
            fake_leecher(tracker_port, h)
            since = time.time()
            def zero_announces():
                stats = get(f"http://127.0.0.1:{tracker_port}/stats")
                later = [e for e in stats['recentAnnounces'] if e['infoHash'] == h
                         and e['time'] > since and e.get('left') == 0 and e['event'] != 'stopped']
                return later if len(later) >= 2 and stats['swarms'][h]['leechers'] >= 1 else None
            later = wait("deux annonces avec un leecher présent", zero_announces)
            assert all(e['uploaded'] == 0 for e in later), later
            assert '127.0.0.1' in get(base + '/api/state', headers)['joalUpload']['announcements'][h]
            # JOAL excludes its own peer: the tracker's "1 seeder" is reported as 0 here.
            wait("compteurs S/L reçus de JOAL", lambda: isinstance(
                (solo_row().get("lastPeers") or {}).get("seeders", {}).get("count"), int))
            series = wait("points du graphique S/L",
                          lambda: get(base + "/api/torrent/peers?infoHash=" + row["infoHash"], headers)["points"])
            print("  dernier point :", series[-1])
            log = data / "runtime" / "solo-seed-sans-upload" / "joal.log"
            wait("journal JOAL écrit via la rotation", lambda: log.exists() and log.stat().st_size > 0)
        else:
            row = wait("refus du client affiché dans la ligne",
                       lambda: (lambda r: r if r and "Client refusé" in (r.get("reason") or "") else None)(solo_row()))
            print("  motif :", row["reason"])
        health = get(base + "/api/health", headers)
        states = {t["name"]: t["status"] for t in health["threads"]}
        print("  santé :", states)
        assert all(states[name] == "ok" for name in ("solo", "swarm", "upload-history", "import-watch")), states
        assert health["version"]
    finally:
        controller.terminate()
        try:
            controller.wait(30)
        except subprocess.TimeoutExpired:
            controller.kill()
        tracker.terminate()
        tracker.wait(10)
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    if not JAR or not CLIENTS:
        raise SystemExit("Définir JOAL_JAR et JOAL_CLIENTS")
    print("Scénario 1 : seed sans upload via le moteur JOAL (profil qBittorrent)")
    scenario(with_profile=True)
    print("Scénario 2 : sans profil .client (script de secours) → refus visible")
    scenario(with_profile=False)
    print("Scénario 3 : Stop → Auto relance un relevé immédiat et démarre la session")
    scenario(with_profile=True, mode_switch=True)
    print("Scénario 4 : torrent importé (règle 0/0 par tracker) en Forced → vrai upload")
    scenario(with_profile=True, forced=True)
    print("Banc d'essai : tous les contrôles sont passés.")
