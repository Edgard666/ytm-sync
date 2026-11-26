#!/usr/bin/env python3
"""Latest inputs of the image, for the weekly GitHub build.

- JOAL: highest x.y.z tag of anthonyraymond/joal on Docker Hub, pinned by digest.
- Java: eclipse-temurin 21 JDK/JRE (jammy), pinned by digest (security updates).
- .client profiles: refreshed from the JOAL repository (its own automation adds the new
  qBittorrent versions there between releases) into ./clients before the build.
- inputs: fingerprint of all of the above plus this repository's code. The workflow does
  not republish an image whose fingerprint is already published (no false "update").

Prints key=value lines for $GITHUB_OUTPUT.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOAL_REPO = 'https://github.com/anthonyraymond/joal.git'
JOAL_TAGS = 'https://hub.docker.com/v2/repositories/anthonyraymond/joal/tags?page_size=100'
BASES = {'jdk_image': 'eclipse-temurin:21-jdk-jammy', 'jre_image': 'eclipse-temurin:21-jre-jammy'}


def latest_version(names):
    """Highest plain x.y.z tag; pre-releases and 'latest' are ignored."""
    versions = [tuple(int(part) for part in name.split('.')) for name in names
                if re.fullmatch(r'\d+\.\d+\.\d+', name)]
    if not versions:
        raise SystemExit('Aucune version x.y.z de JOAL trouvée sur Docker Hub')
    return '.'.join(str(part) for part in max(versions))


def parse_digest(text):
    match = re.search(r'^Digest:\s+(sha256:[0-9a-f]{64})\s*$', text, re.MULTILINE)
    if not match:
        raise SystemExit('Empreinte introuvable dans la réponse du registre')
    return match.group(1)


def digest(reference):
    output = subprocess.run(['docker', 'buildx', 'imagetools', 'inspect', reference],
                            capture_output=True, text=True, check=True).stdout
    return f'{reference}@{parse_digest(output)}'


def clients_hash(folder):
    h = hashlib.sha256()
    for path in sorted(Path(folder).glob('*.client')):
        h.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return h.hexdigest()


def fingerprint(code_tree, joal_image, jdk_image, jre_image, profiles):
    return hashlib.sha256('\n'.join((code_tree, joal_image, jdk_image, jre_image, profiles)).encode()).hexdigest()[:32]


def refresh_clients(target):
    """Mirror the upstream profiles into target (the build context only, never /data)."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(['git', 'clone', '--quiet', '--depth', '1', JOAL_REPO, tmp + '/joal'], check=True)
        upstream = Path(tmp) / 'joal' / 'resources' / 'clients'
        profiles = sorted(upstream.glob('*.client'))
        if len(profiles) < 20:
            raise SystemExit(f'Profils JOAL inattendus ({len(profiles)}) : construction annulée')
        for path in profiles:
            json.loads(path.read_text(encoding='utf-8'))          # refuse a broken profile
        for old in Path(target).glob('*.client'):
            old.unlink()
        for path in profiles:
            shutil.copy2(path, Path(target) / path.name)
        shutil.copy2(Path(tmp) / 'joal' / 'LICENSE', Path(target) / 'LICENSE-JOAL')
        return len(profiles)


def main():
    with urllib.request.urlopen(JOAL_TAGS, timeout=30) as response:
        tags = [item['name'] for item in json.load(response).get('results', [])]
    version = latest_version(tags)
    values = {'joal_version': version, 'joal_image': digest(f'anthonyraymond/joal:{version}')}
    values.update({key: digest(reference) for key, reference in BASES.items()})
    count = refresh_clients(ROOT / 'clients')
    tree = subprocess.run(['git', 'rev-parse', 'HEAD^{tree}'], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()
    values['inputs'] = fingerprint(tree, values['joal_image'], values['jdk_image'], values['jre_image'],
                                   clients_hash(ROOT / 'clients'))
    values['app_version'] = re.search(r'APP_VERSION = "([^"]+)"', (ROOT / 'app' / 'version.py').read_text()).group(1)
    values['profiles'] = str(count)
    for key, value in values.items():
        print(f'{key}={value}')
    print(f'JOAL {version}, {count} profils, empreinte {values["inputs"]}', file=sys.stderr)


if __name__ == '__main__':
    main()
