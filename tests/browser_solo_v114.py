"""Offline Chromium check of JOAL-only import presentation and metadata list."""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'app/static'
conf=json.loads((ROOT/'config.example.json').read_text())
conf['rules'][0]['enabled']=True
state={'qbitConnected':False,'dryRun':True,'error':None,'rules':[],
  'torrents':[], 'totals':{},'cleanup':{},'swarm':{},
  'solo':{'count':2,'active':1,'items':[
    {'name':'Torrent indépendant 1','infoHash':'a'*40,'trackers':['tracker.example.org'],
     'rule':'tracker-exemple','leechers':15,'threshold':4,'active':True,'reason':'Session JOAL active'},
    {'name':'Torrent indépendant 2','infoHash':'b'*40,'trackers':['tracker.example.org'],
     'rule':'tracker-exemple','leechers':0,'threshold':4,'active':False,'reason':'Leechers insuffisants : 0 / minimum 4'}]}}
fixtures={'/api/auth/session':{'authenticated':True,'csrfToken':'token'},'/api/config':conf,
          '/api/state':state,'/api/clients':{'clients':[]}}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1250,'height':850})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
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
    page.locator('.nav-link[data-view="import"]').click()
    page.wait_for_function("document.querySelectorAll('#solo-items tr').length===2")
    assert '2 torrent(s) · 1 actif(s)' in page.locator('#solo-count').inner_text()
    assert 'Leechers insuffisants' in page.locator('#solo-items').inner_text()
    assert page.locator('#import-submit').inner_text() == 'Ajouter à JOAL uniquement'
    assert 'Ajouter à qBittorrent' not in page.locator('#import-form').inner_text()
    # Une action « Retirer » par ligne (les lignes ont depuis aussi les modes, le diagnostic et le graphique).
    assert page.locator('#solo-items button', has_text='Retirer').count()==2
    assert not errors,errors
    print('Chromium v1.14: bibliothèque JOAL uniquement et états OK')
    browser.close()
