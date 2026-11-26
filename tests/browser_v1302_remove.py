"""Chromium v1.30.2: « Retirer de JOAL » removes only the selected imported torrents."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
conf = json.loads((ROOT / 'config.example.json').read_text())
conf['rules'][0]['enabled'] = True
solo = lambda name, h: {'name': name, 'infoHash': h, 'trackers': ['tracker.example.org'], 'rule': 'tracker-exemple',
                        'leechers': 3, 'threshold': 0, 'active': False, 'reason': 'En attente'}
state = {'qbitConnected': True, 'dryRun': True, 'error': None, 'rules': [],
         'torrents': [{'name': 'qBit exemple', 'infoHash': 'd' * 40, 'hash': 'd' * 12, 'trackers': ['tracker.example.org'],
                       'rule': 'tracker-exemple', 'qbitState': 'uploading', 'progress': 100, 'stopped': False,
                       'ratio': 0.6, 'leechers': 6, 'selected': False, 'eligible': False, 'reason': 'qBittorrent actif',
                       'upload': {'uploadedBytes': 100, 'targetBytes': 1000, 'percent': 10}}],
         'totals': {'tagged': 1, 'selected': 0, 'eligible': 0, 'blocked': 1}, 'cleanup': {}, 'swarm': {},
         'solo': {'count': 3, 'active': 0, 'items': [solo('Importé A', 'a' * 40), solo('Importé B', 'b' * 40),
                                                     solo('Importé C', 'c' * 40)]}}
fixtures = {'/api/auth/session': {'authenticated': True, 'csrfToken': 'token'}, '/api/config': conf,
            '/api/state': state, '/api/clients': {'clients': []}}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM', '/usr/bin/chromium'), args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    html = (STATIC / 'index.html').read_text()
    for tag in ['<link rel="stylesheet" href="/assets/app.css">', '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>']:
        html = html.replace(tag, '')
    page.set_content(html)
    page.add_style_tag(content=(STATIC / 'app.css').read_text())
    page.evaluate('''fixtures => {
      window.deleted = [];
      window.fetch = async (path, options) => {
        if (path === '/api/solo/delete') {
          const body = JSON.parse(options.body); window.deleted.push(body);
          fixtures['/api/state'].solo.items = fixtures['/api/state'].solo.items.filter(r => !body.infoHashes.includes(r.infoHash));
          fixtures['/api/state'].solo.count = fixtures['/api/state'].solo.items.length;
          return new Response(JSON.stringify({success: true, removed: body.infoHashes, missing: []}),
                              {status: 200, headers: {'Content-Type': 'application/json'}});
        }
        const data = fixtures[path];
        return new Response(JSON.stringify(data || {error: 'missing'}), {status: data ? 200 : 404,
                            headers: {'Content-Type': 'application/json'}});
      };}''', fixtures)
    page.add_script_tag(content=(STATIC / 'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC / 'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
    page.wait_for_function("document.querySelectorAll('#overview-items input[data-mode-hash]').length === 4")

    home = page.locator('#view-dashboard [data-batch-remove]')
    assert home.is_disabled() and home.inner_text() == 'Retirer de JOAL'
    # Mixed selection on the home page: two imported torrents and the qBittorrent one.
    for h in ('a', 'c', 'd'):
        page.locator(f'#overview-items input[data-mode-hash="{h * 40}"]').check()
    assert home.is_enabled() and home.inner_text() == 'Retirer de JOAL (2)', home.inner_text()
    prompts = []
    page.on('dialog', lambda d: (prompts.append(d.message), d.accept()))
    home.click()
    page.wait_for_function("window.deleted.length === 1")
    sent = page.evaluate("() => window.deleted[0]")
    assert sorted(sent['infoHashes']) == ['a' * 40, 'c' * 40], sent             # qBittorrent torrent never sent
    assert 'Retirer 2 torrent(s) importé(s)' in prompts[0] and '1 torrent(s) qBittorrent' in prompts[0], prompts
    page.wait_for_function("document.querySelectorAll('#overview-items input[data-mode-hash]').length === 2")
    # The qBittorrent torrent stays selected, nothing removable remains selected.
    assert page.locator('#overview-items input[data-mode-hash="' + 'd' * 40 + '"]').is_checked()
    assert home.is_disabled() and home.inner_text() == 'Retirer de JOAL'

    # Import view: « Sélectionner tout » then remove, cancelled confirmation removes nothing.
    page.locator('.nav-link[data-view="import"]').click()
    page.wait_for_function("document.querySelectorAll('#solo-items tr').length === 1")
    page.locator('[data-toggle-all="import"]').click()
    imported = page.locator('#view-import [data-batch-remove]')
    assert imported.inner_text() == 'Retirer de JOAL (1)', imported.inner_text()
    assert not errors, errors
    print('Chromium v1.30.2: retrait groupé des torrents importés uniquement, sélection mixte OK')
    browser.close()
