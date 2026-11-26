"""Chromium smoke check: multifile selection, automatic analysis and no confirmation."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
config = json.loads((ROOT / 'config.example.json').read_text())
state = {'qbitConnected':True,'dryRun':True,'rules':[], 'torrents':[],
         'totals':{'tagged':0,'selected':0,'eligible':0,'blocked':0},
         'solo':{'count':0,'items':[]},'cleanup':{},'swarm':{}}
fixtures = {'/api/auth/session':{'authenticated':True,'csrfToken':'token'},
            '/api/config':config, '/api/state':state, '/api/clients':{'clients':[]}}
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),
                                args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':1280,'height':850})
    errors=[]
    page.on('pageerror', lambda e: errors.append(str(e)))
    html=(STATIC/'index.html').read_text()
    for tag in ['<link rel="stylesheet" href="/assets/app.css">',
                '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>']:
        html=html.replace(tag,'')
    page.set_content(html)
    page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''fixtures => {
       window.inspected = [];
       window.imported = [];
       window.confirm = () => { throw new Error('Unexpected confirmation'); };
       window.fetch = async (url, req={}) => {
         if (url === '/api/import/inspect') {
           const names = req.body.getAll('torrent').map(file => file.name);
           window.inspected.push(names);
           if (names.includes('slow.torrent')) await new Promise(r=>setTimeout(r,380));
           return new Response(JSON.stringify({results: names.map((name,i)=>({filename:name,
             name:name,hash:'a'.repeat(40),domains:['tracker.example.org'],
             matchedDomains:['tracker.example.org'],status:name.startsWith('bad')?'error':'ok',
             message:name.startsWith('bad')?'Fichier invalide':undefined}))}),
             {status:200,headers:{'Content-Type':'application/json'}});
         }
         if (url === '/api/import/add') {
           const names = req.body.getAll('torrent').map(file=>file.name);
           window.imported.push(names);
           return new Response(JSON.stringify({results:names.map((name,i)=>({filename:name,
             name:name,hash:'a'.repeat(40),status:i===0?'added':i===1?'duplicate':'error',
             message:i===0?'Enregistré':i===1?'Déjà présent':'Invalide'}))}),
             {status:200,headers:{'Content-Type':'application/json'}});
         }
         const data=fixtures[url];
         return new Response(JSON.stringify(data || {error:'not found'}),
           {status:data?200:404,headers:{'Content-Type':'application/json'}});
       };
    }''',fixtures)
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.locator('.nav-link[data-view="import"]').click()
    assert page.locator('#import-file').evaluate('(e)=>e.multiple')
    assert page.locator('#import-submit').is_disabled()
    page.locator('#import-file').set_input_files([
       {'name':'one.torrent','mimeType':'application/x-bittorrent','buffer':b'abc'},
       {'name':'two.torrent','mimeType':'application/x-bittorrent','buffer':b'def'},
       {'name':'bad.torrent','mimeType':'application/x-bittorrent','buffer':b'ghi'}])
    page.wait_for_function("document.querySelector('#import-submit').disabled === false")
    assert len(page.locator('#import-preview .import-item').all()) == 3
    page.locator('#import-submit').click()
    page.wait_for_function('window.imported.length === 1')
    assert page.evaluate('window.imported[0]') == ['one.torrent','two.torrent','bad.torrent']
    # Le message est écrit après la réponse de l'import (asynchrone).
    page.wait_for_function("document.querySelector('#import-message').textContent.includes('1 importé(s), 1 doublon(s), 1 erreur(s).')")
    assert len(page.locator('#import-preview .import-item').all()) == 3
    assert page.locator('#import-file').input_value()==''
    # stale delayed inspection must not overwrite a later selection
    page.locator('#import-file').set_input_files({'name':'slow.torrent','mimeType':'application/x-bittorrent','buffer':b'abc'})
    page.locator('#import-file').set_input_files({'name':'new.torrent','mimeType':'application/x-bittorrent','buffer':b'def'})
    page.wait_for_function("document.querySelector('#import-submit').disabled === false")
    page.wait_for_timeout(500)
    assert 'new.torrent' in page.locator('#import-preview').inner_text()
    assert 'slow.torrent' not in page.locator('#import-preview').inner_text()
    assert not errors, errors
    print('Chromium v1.16: sélection multiple, analyse auto, résultats partiels et absence de confirmation OK')
    browser.close()
