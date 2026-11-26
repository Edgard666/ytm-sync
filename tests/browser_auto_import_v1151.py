"""Chromium: auto-inspection, direct add without confirm, stale async response protection."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
config = json.loads((ROOT / 'config.example.json').read_text())
state = {'qbitConnected': True, 'dryRun': True, 'rules': [], 'torrents': [],
         'totals': {'tagged': 0, 'selected': 0, 'eligible': 0, 'blocked': 0},
         'solo': {'count': 0, 'items': []}, 'cleanup': {}, 'swarm': {}}
fixtures = {'/api/auth/session': {'authenticated': True, 'csrfToken': 'token'},
            '/api/config': config, '/api/state': state, '/api/clients': {'clients': []}}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM', '/usr/bin/chromium'),
                                args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1250, 'height': 850})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    html = (STATIC / 'index.html').read_text()
    for tag in ['<link rel="stylesheet" href="/assets/app.css">',
                '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>']:
        html = html.replace(tag, '')
    page.set_content(html)
    page.add_style_tag(content=(STATIC / 'app.css').read_text())
    page.evaluate('''fixtures => {
      window.inspectedNames = [];
      window.importedNames = [];
      window.confirm = () => {throw Error('Confirmation inattendue');};
      window.fetch = async (url, req={}) => {
        if (url === '/api/import/inspect') {
          const file = req.body.get('torrent');
          window.inspectedNames.push(file.name);
          if (file.name.includes('ancienne')) await new Promise(r => setTimeout(r, 400));
          if (file.name.includes('invalide'))
            return new Response(JSON.stringify({error:'Métadonnées incorrectes'}),
              {status:400,headers:{'Content-Type':'application/json'}});
          return new Response(JSON.stringify({torrent:{name:file.name,hash:'a'.repeat(40),
             domains:['tracker.example.org'],matchedDomains:['tracker.example.org']}}),
             {status:200,headers:{'Content-Type':'application/json'}});
        }
        if (url === '/api/import/add') {
          window.importedNames.push(req.body.get('torrent').name);
          return new Response(JSON.stringify({message:'Enregistré dans /data/torrents'}),
             {status:200,headers:{'Content-Type':'application/json'}});
        }
        const data = fixtures[url];
        return new Response(JSON.stringify(data || {error:'not found'}),
          {status: data ? 200 : 404,headers:{'Content-Type':'application/json'}});
      };
    }''', fixtures)
    page.add_script_tag(content=(STATIC / 'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC / 'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.locator('.nav-link[data-view="import"]').click()
    assert page.locator('#import-inspect').count() == 0
    # v1.16 : import groupé (plusieurs fichiers) – remplace le fichier unique de la v1.15.1.
    assert page.locator('#import-file').evaluate('(e) => e.multiple')
    assert page.locator('#import-submit').is_disabled()

    def upload(name):
        page.locator('#import-file').set_input_files({
          'name': name, 'mimeType': 'application/x-bittorrent', 'buffer': b'abc123'})

    upload('premier.torrent')
    page.wait_for_function("document.querySelector('#import-submit').disabled === false")
    assert 'premier.torrent' in page.locator('#import-preview').inner_text()
    page.locator('#import-submit').click()
    page.wait_for_function("window.importedNames.length === 1")
    assert page.evaluate('window.importedNames') == ['premier.torrent']
    assert page.locator('#import-submit').is_disabled()
    # v1.16 : les résultats restent affichés après l'enregistrement.
    assert 'premier.torrent' in page.locator('#import-preview').inner_text()

    upload('ancienne.torrent')
    upload('nouvelle.torrent')
    page.wait_for_function("document.querySelector('#import-submit').disabled === false")
    page.wait_for_timeout(600)
    assert 'nouvelle.torrent' in page.locator('#import-preview').inner_text()
    assert 'ancienne.torrent' not in page.locator('#import-preview').inner_text()
    page.locator('#import-submit').click()
    page.wait_for_function("window.importedNames.length === 2")
    assert page.evaluate('window.importedNames') == ['premier.torrent', 'nouvelle.torrent']

    upload('invalide.torrent')
    page.wait_for_function("document.querySelector('#import-message').textContent.includes('Métadonnées incorrectes')")
    assert page.locator('#import-submit').is_disabled()
    # Depuis la v1.16, le fichier invalide reste sélectionné avec son erreur ; l'envoi reste bloqué.
    assert not errors, errors
    print('Chromium v1.15.1 : analyse automatique, import sans confirmation, changement de fichier et erreur OK')
    browser.close()
