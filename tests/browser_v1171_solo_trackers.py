"""Browser: imported solo torrent tracker opens the rule picker and persists."""
from pathlib import Path
import json, os
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'app/static'
H='a'*40
config=json.loads((ROOT/'config.example.json').read_text())
config['rules']=[dict(id='tracker-default',enabled=True,trackerDomains=['old.example.org'],minLeechers=4,
                      minUploadRate=10,maxUploadRate=200,simultaneousSeed=10,targetRatio=0,client='test.client'),
                 dict(id='zero',enabled=True,trackerDomains=['elsewhere.example.org'],minLeechers=0,
                      minUploadRate=10,maxUploadRate=200,simultaneousSeed=10,targetRatio=0,client='test.client')]
solo={'infoHash':H,'hash':H[:12],'name':'Atlas King','mode':'JOAL uniquement','trackers':['announce.v3x.club','tk.v3x.tw'],
      'rule':None,'leechers':None,'threshold':None,'reason':'Aucune règle active ne couvre les trackers',
      'active':False,'selected':False,'addedOn':1700000000}
state={'qbitConnected':True,'dryRun':True,'error':None,
       'torrents':[], 'solo':{'count':1,'active':0,'items':[solo]},'rules':[],
       'swarm':{},'cleanup':{},'totals':{'tagged':0,'selected':0,'eligible':0,'blocked':0,'evaluated':0}}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1350,'height':900})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    html=(STATIC/'index.html').read_text()
    for tag in ('<link rel="stylesheet" href="/assets/app.css">',
                '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>'):
        html=html.replace(tag,'')
    page.set_content(html)
    page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''({config,state})=>{
      window.__fixtures={'/api/auth/session':{authenticated:true,csrfToken:'test'},
                         '/api/config':config,'/api/state':state,'/api/clients':{clients:[]}};
      window.__saves=[];
      window.fetch=async (path,options={})=>{
        if(path==='/api/config'&&options.method==='POST'){
          const body=JSON.parse(options.body);window.__saves.push(body);
          window.__fixtures[path]=body;
          return new Response(JSON.stringify({ok:true}),{headers:{'Content-Type':'application/json'}});
        }
        const value=window.__fixtures[path];
        return new Response(JSON.stringify(value||{error:'missing'}),
          {status:value?200:404,headers:{'Content-Type':'application/json'}});
      }
    }''', {'config':config,'state':state})
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.locator('.nav-link[data-view="import"]').click()
    page.wait_for_function("document.querySelectorAll('#solo-items button.tracker-assign').length===2")
    buttons=page.locator('#solo-items button.tracker-assign')
    assert buttons.count()==2, buttons.count()
    buttons.first.click()
    dialog=page.locator('.assign-trackers-dialog')
    assert dialog.is_visible()
    assert 'announce.v3x.club' in dialog.inner_text()
    dialog.locator('select').select_option('zero')
    dialog.get_by_role('button',name='Ajouter à la règle').click()
    page.wait_for_function('window.__saves.length===1')
    saved=page.evaluate('window.__saves[0]')
    assert 'announce.v3x.club' in saved['rules'][1]['trackerDomains'], saved
    assert saved['rules'][0]['trackerDomains']==['old.example.org']
    assert 'tk.v3x.tw' not in saved['rules'][1]['trackerDomains']
    assert not errors,errors
    print('Chromium: solo tracker clickable, selectable rule, saved without modifying other rules')
    browser.close()
