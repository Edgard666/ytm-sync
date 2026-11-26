"""Offline browser smoke for v1.13 views. No tracker announces are sent."""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'app/static'
info='b'*40
config=json.loads((ROOT/'config.example.json').read_text())
state={'qbitConnected':True,'dryRun':True,'error':None,'totals':{'tagged':1,'selected':0,'eligible':0,'blocked':1,'evaluated':1},
       'torrents':[],'rules':[],'cleanup':{},'swarm':{'count':1,'items':[{'name':'Torrent example','infoHash':info,'trackers':['tracker.example.org'],
       'rules':{'tracker-exemple':{'leechers':12,'status':'récent','sampledAt':'2026-11-17T15:00:00+00:00','source':'scrape UDP'}}}]}}
points=[{'time':f'2026-11-17T{hr:02d}:00:00+00:00','leechers':value,'status':'ok'} for hr,value in [(10,7),(11,9),(12,12),(14,8)]]
points.insert(3,{'time':'2026-11-17T13:00:00+00:00','leechers':None,'status':'scrape indisponible'})
fixtures={'/api/auth/session':{'authenticated':True,'csrfToken':'test'},'/api/config':config,'/api/state':state,
          '/api/clients':{'clients':[]},'/api/notifications':{'items':[{'message':'Connexion qBittorrent interrompue','level':'error','read':False,'at':'2026-11-17T10:00:00+00:00'},
              {'message':'Connexion qBittorrent rétablie','level':'success','read':False,'at':'2026-11-17T10:10:00+00:00'}],'unread':2},
          '/api/swarm/history?infoHash='+info+'&rule=tracker-exemple':{'name':'Torrent example','points':points,'retentionDays':14,'trackerSeries':{'tracker.example.org':points}}}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'), args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1320,'height':840})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    html=(STATIC/'index.html').read_text()
    for tag in ['<link rel="stylesheet" href="/assets/app.css">','<script src="/assets/torrent_table.js" defer></script>', '<script src="/assets/app.js" defer></script>']:
        html=html.replace(tag,'')
    page.set_content(html)
    page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''fixtures => {window.fetch=async (path,opt={}) => {
        if(path==='/api/notifications/read')return new Response(JSON.stringify({read:2}),{status:200,headers:{'Content-Type':'application/json'}});
        if(path==='/api/backup/export')return new Response(new Blob(['ZIP'],{type:'application/zip'}),{status:200,headers:{'Content-Type':'application/zip'}});
        const data=fixtures[path];return new Response(JSON.stringify(data||{error:'not found'}),{status:data?200:404,headers:{'Content-Type':'application/json'}});
    };}''',fixtures)
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.wait_for_function("document.querySelector('#notifications-count').textContent==='2'")
    page.locator('#notifications-toggle').click()
    assert 'Connexion qBittorrent interrompue' in page.locator('#notification-items').inner_text()
    page.locator('#notification-close').click()
    page.locator('.nav-link[data-view="swarm"]').click()
    page.locator('#swarm-items button').first.click()
    page.wait_for_function("document.querySelectorAll('#swarm-history-chart svg circle').length===4")
    assert '4 relevé(s)' in page.locator('#swarm-history-description').inner_text()
    page.locator('#swarm-history-series').select_option('tracker.example.org')
    assert page.locator('#swarm-history-chart svg circle').count() == 4
    page.locator('#swarm-history-close').click()
    page.locator('.nav-link[data-view="backup"]').click()
    assert page.locator('#backup-export').is_visible()
    assert page.locator('#backup-restore').is_visible()
    assert not errors,errors
    print('Chromium: notifications, interactive scrape chart, backup controls OK')
    browser.close()
