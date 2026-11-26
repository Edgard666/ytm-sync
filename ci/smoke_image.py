#!/usr/bin/env python3
"""End-to-end check of a built image before it is published.

1. JOAL (with this project's telemetry patch) announces a test torrent to a local fake
   tracker using a real .client profile, and the telemetry file reports the tracker's
   seeders/leechers. A new JOAL release that breaks the patch fails here.
2. The controller starts, answers /healthz, and copies the bundled .client profiles into
   an empty /data/clients.

Usage:  python3 ci/smoke_image.py --image ghcr.io/user/joal-smart-controller:candidate
        python3 ci/smoke_image.py --jar /path/to/patched/joal.jar     (part 1 only, no Docker)
"""
import argparse
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from core import encode_bencode  # noqa: E402

SEEDERS, LEECHERS = 7, 3


def fake_tracker():
    seen = []

    class Tracker(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({'path': self.path, 'agent': self.headers.get('User-Agent', '')})
            body = encode_bencode({b'interval': 1800, b'complete': SEEDERS, b'incomplete': LEECHERS, b'peers': b''})
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Tracker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_torrent(port):
    info = {b'name': b'joal-smoke-test', b'length': 734003200, b'piece length': 1048576,
            b'pieces': hashlib.sha1(b'joal-smoke').digest() * 700}
    blob = encode_bencode({b'announce': f'http://127.0.0.1:{port}/announce'.encode(), b'info': info})
    return blob, hashlib.sha1(encode_bencode(info)).hexdigest()


def pick_profile(clients):
    names = sorted(p.name for p in clients.glob('qbittorrent-*.client'))
    if not names:
        raise SystemExit('Aucun profil qbittorrent-*.client dans ' + str(clients))
    return names[-1]


def joal_check(run_joal, clients):
    server, seen = fake_tracker()
    work = Path(tempfile.mkdtemp(prefix='joal-smoke-'))
    proc = None
    try:
        conf, tele = work / 'conf', work / 'tele'
        (conf / 'torrents').mkdir(parents=True)
        tele.mkdir()
        shutil.copytree(clients, conf / 'clients')
        profile = pick_profile(conf / 'clients')
        (conf / 'config.json').write_text(json.dumps({
            'minUploadRate': 30, 'maxUploadRate': 160, 'simultaneousSeed': 1, 'client': profile,
            'keepTorrentWithZeroLeechers': True, 'uploadRatioTarget': -1.0}))
        blob, infohash = test_torrent(server.server_port)
        (conf / 'torrents' / 'smoke.torrent').write_bytes(blob)
        os.chmod(work, 0o777)
        for folder in (conf, tele, conf / 'torrents', conf / 'clients'):
            os.chmod(folder, 0o777)
        proc = run_joal(conf, tele)
        deadline = time.time() + 120
        row = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise SystemExit('ÉCHEC : JOAL s’est arrêté (code %s)' % proc.returncode)
            try:
                row = json.loads((tele / 'upload.json').read_text()).get(infohash)
            except (OSError, ValueError):
                row = None
            # JOAL leaves itself out of the seeders count reported by the tracker.
            if row and len(row) >= 4 and row[2] in (SEEDERS, SEEDERS - 1) and row[3] == LEECHERS:
                break
            time.sleep(1)
        else:
            raise SystemExit(f'ÉCHEC : télémétrie absente ou incomplète après 120 s (dernière valeur : {row})')
        announce = next((s for s in seen if 'info_hash=' in s['path']), None)
        if announce is None:
            raise SystemExit('ÉCHEC : aucune annonce reçue par le faux tracker')
        profile_data = json.loads((conf / 'clients' / profile).read_text())
        agent = next((h['value'] for h in profile_data['requestHeaders'] if h['name'].lower() == 'user-agent'), '')
        if agent and announce['agent'] != agent:
            raise SystemExit(f'ÉCHEC : User-Agent {announce["agent"]!r} au lieu de {agent!r}')
        print(f'OK JOAL : annonce reçue avec le profil {profile}, télémétrie {row[2]} seeders / {row[3]} leechers, '
              f'{row[0]} o/s simulés')
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        server.shutdown()
        server.server_close()
        shutil.rmtree(work, ignore_errors=True)


def controller_check(image):
    data = Path(tempfile.mkdtemp(prefix='joal-smoke-data-'))
    os.chmod(data, 0o777)
    name = 'joal-smoke-%d' % os.getpid()
    try:
        subprocess.run(['docker', 'run', '-d', '--name', name, '--network', 'host', '-e', 'PORT=18788',
                        '-e', 'ADMIN_PASSWORD=smoke-test-password-123', '-e', 'IMPORT_WATCH=0',
                        '-v', f'{data}:/data', image], check=True, stdout=subprocess.DEVNULL)
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:18788/healthz', timeout=3) as response:
                    health = json.load(response)
                break
            except OSError:
                time.sleep(2)
        else:
            logs = subprocess.run(['docker', 'logs', '--tail', '60', name], capture_output=True, text=True)
            raise SystemExit('ÉCHEC : le contrôleur ne répond pas\n' + logs.stdout + logs.stderr)
        copied = len(list((data / 'clients').glob('*.client')))
        if copied == 0:
            raise SystemExit('ÉCHEC : aucun profil .client copié dans /data/clients')
        print(f'OK contrôleur : version {health.get("version")}, {copied} profils .client copiés')
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'sh', '-v', f'{data}:/data', image, '-c',
                        'rm -rf /data/* /data/.[!.]*'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(data, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--image')
    group.add_argument('--jar')
    args = parser.parse_args()
    java = ['-Xmx256m', '-XX:+ExitOnOutOfMemoryError']
    if args.jar:
        env = {k: v for k, v in os.environ.items() if k not in ('_JAVA_OPTIONS', 'JAVA_TOOL_OPTIONS')}

        def run_joal(conf, tele):
            return subprocess.Popen(['java', *java, f'-Dcontroller.telemetry={tele}/upload.json', '-jar', args.jar,
                                     f'--joal-conf={conf}'], cwd=conf, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        joal_check(run_joal, ROOT / 'clients')
        return

    def run_joal(conf, tele):
        return subprocess.Popen(['docker', 'run', '--rm', '--network', 'host', '--entrypoint', 'java',
                                 '-v', f'{conf}:/conf', '-v', f'{tele}:/tele', '-w', '/conf', args.image,
                                 *java, '-Dcontroller.telemetry=/tele/upload.json', '-jar', '/opt/joal/joal.jar',
                                 '--joal-conf=/conf'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    joal_check(run_joal, ROOT / 'clients')
    controller_check(args.image)


if __name__ == '__main__':
    main()
