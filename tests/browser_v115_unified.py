"""Chromium: solo + qBittorrent on dashboard and sortable, filterable table."""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'app/static'
config=json.loads((ROOT/'config.example.json').read_text())
config['rules'][0]['enabled']=True
h='b'*40
state={'qbitConnected':False,'dryRun':True,'error':None,'rules':[],
  'torrents':[{'name':'qBit exemple','infoHash':'a'*40,'hash':'a'*12,
    'trackers':['tracker.example.org'],'rule':'tracker-exemple','qbitState':'uploading',
    'progress':100,'stopped':False,'ratio':0.6,'leechers':6,'selected':False,
    'eligible':False,'reason':'qBittorrent actif','upload':{'uploadedBytes':100,'targetBytes':1000,'percent':10}}],
  'totals':{'tagged':1,'selected':0,'eligible':0,'blocked':1},'cleanup':{},
  'swarm':{'items':[{'infoHash':h,'name':'Autonome exemple','trackers':['tracker.example.org'],
    'rules':{'tracker-exemple':{'status':'indisponible','sampledAt':None,'attemptedAt':'2026-11-17T22:00:00+00:00',
      'trackerCount':1,'successCount':0,'leechers':None,
      'trackerDiagnostics':[{'domain':'tracker.example.org','state':'error','reason':'Réponse HTTP 403 (accès refusé)','leechers':None}]}}}]},
  'solo':{'count':1,'active':0,'items':[{'name':'Autonome exemple','infoHash':h,'hash':h[:12],
    'trackers':['tracker.example.org'],'rule':'tracker-exemple',
    'leechers':None,'threshold':4,'active':False,'selected':False,
    'addedOn':1760000000,'reason':'Leechers inconnus : relevé refusé'}]}}
fixtures={'/api/auth/session':{'authenticated':True,'csrfToken':'token'},'/api/config':config,
          '/api/state':state,'/api/clients':{'clients':[]}}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1300,'height':900})
    errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
    html=(STATIC/'index.html').read_text()
    for tag in ['<link rel="stylesheet" href="/assets/app.css">',
                '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>']:
        html=html.replace(tag,'')
    page.set_content(html)
    page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''fixtures => {window.fetch=async path => {
       const data=fixtures[path]; return new Response(JSON.stringify(data||{error:'missing'}),
       {status:data?200:404,headers:{'Content-Type':'application/json'}});
    };}''',fixtures)
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.wait_for_function("document.querySelectorAll('#overview-items tr').length===2")
    assert 'JOAL uniquement' in page.locator('#overview-items').inner_text()
    assert 'qBittorrent' in page.locator('#overview-items').inner_text()
    assert page.locator('#kpi-solo').inner_text()=='1'
    # Ligne visée par son nom : l'ordre par défaut du tableau a changé depuis la v1.15.
    page.locator('#overview-items tr', has_text='Autonome exemple').locator('.tracker-diagnostic-button').click()
    dialog=page.locator('.tracker-diagnostic-dialog')
    assert dialog.is_visible()
    assert 'Réponse HTTP 403' in dialog.inner_text()
    assert 'tracker.example.org' in dialog.inner_text()
    assert 'passkey' not in dialog.inner_text()
    dialog.get_by_role('button',name='Fermer').click()
    page.locator('.nav-link[data-view="torrents"]').click()
    page.wait_for_function("document.querySelectorAll('#torrent-items tr').length===2")
    assert 'Autonome exemple' in page.locator('#torrent-items').inner_text()
    assert 'qBit exemple' in page.locator('#torrent-items').inner_text()
    page.select_option('#torrent-mode','solo')
    assert page.locator('#torrent-items tr').count()==1
    assert 'Autonome exemple' in page.locator('#torrent-items').inner_text()
    page.select_option('#torrent-mode','qbit')
    assert page.locator('#torrent-items tr').count()==1
    assert 'qBit exemple' in page.locator('#torrent-items').inner_text()
    page.select_option('#torrent-mode','all')
    assert not errors, errors
    print('Chromium v1.15: vue unifiée, filtre mode et diagnostic par tracker OK')
    browser.close()
