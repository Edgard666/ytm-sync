"""Chromium smoke test: combined swarm statistics and honest JOAL activity bar."""
import json, os
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]; STATIC=ROOT/'app/static'
conf=json.loads((ROOT/'config.example.json').read_text()); conf['rules'][0]['enabled']=True
h='a'*40
solo={'infoHash':h,'hash':h[:12],'name':'Solo Demo','sizeBytes':2097152,'trackers':['track.example.org'],
      'rule':conf['rules'][0]['id'],'leechers':30,'seeders':15,'threshold':4,
      'active':True,'selected':True,'addedOn':1700000000,
      'joalSession':{'active':True,'uptimeSeconds':1234,'simulatedBytes':None},
      'source':'scrape HTTP(S)/UDP','reason':'Session JOAL active'}
state={'qbitConnected':True,'dryRun':False,'error':None,'torrents':[],
       'solo':{'count':1,'active':1,'items':[solo]},'rules':[],'swarm':{'items':[]},
       'cleanup':{},'totals':{'tagged':0,'selected':0,'eligible':0,'blocked':0}}
state['joalUpload'] = {'bytesPerSecond':120000, 'byInfoHash':{h:120000}, 'uploadedBytes':{h:1048576}, 'totalUploadedBytes':{h:1048576}}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1300,'height':900});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    html=(STATIC/'index.html').read_text()
    for tag in ['<link rel="stylesheet" href="/assets/app.css">',
       '<script src="/assets/torrent_table.js" defer></script>',
       '<script src="/assets/app.js" defer></script>']:
        html=html.replace(tag,'')
    page.set_content(html);page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''({config,state})=>{window.fetch=async path=>{
      const map={'/api/auth/session':{authenticated:true,csrfToken:'x'},'/api/config':config,
                 '/api/state':state,'/api/clients':{clients:[]}};
      const data=map[path];return new Response(JSON.stringify(data||{error:'missing'}),
        {status:data?200:404,headers:{'Content-Type':'application/json'}});
    }}''',{'config':conf,'state':state})
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.wait_for_function("document.querySelector('#overview-items').innerText.includes('S 15 · L 30')")
    assert 'Fichiers : 2 Mio · Ratio session 0,5 ×' in page.locator('#overview-items').inner_text()
    page.locator('.nav-link[data-view="torrents"]').click()
    page.wait_for_function("document.querySelector('#torrent-items').innerText.includes('S 15 · L 30')")
    assert page.locator('#torrent-items progress').evaluate('(bar) => bar.value') == 50
    page.locator('#torrent-scroll-right').click()
    offset = page.locator('#torrent-table-scroll').evaluate('(e) => e.scrollLeft')
    assert offset > 100
    page.wait_for_timeout(1200)
    assert page.locator('#torrent-table-scroll').evaluate('(e) => e.scrollLeft') == offset
    assert '1 Mio simulés cette session' in page.locator('#torrent-items .upload-cell').inner_text()
    assert '120 ko/s' in page.locator('#torrent-items .upload-cell').inner_text()
    assert '120 ko/s' in page.locator('#torrent-joal-upload').inner_text()
    page.locator('.nav-link[data-view="import"]').click()
    assert '1 Mio simulés cette session' in page.locator('#solo-items').inner_text()
    assert not errors,errors
    browser.close()
    print('Chromium: compteurs S/L, progression simulée mesurée et compteurs de session OK')
