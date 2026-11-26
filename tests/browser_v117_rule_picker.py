"""Chromium smoke: click rule cell and assign policy independently per torrent."""
from pathlib import Path
import json, os
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
STATIC=ROOT/'app/static'
H='a'*40; H2='b'*40
cfg=json.loads((ROOT/'config.example.json').read_text())
cfg['rules']=[dict(id='tracker-default',enabled=True,trackerDomains=['example.org'],minLeechers=3,
                   minUploadRate=10,maxUploadRate=100,simultaneousSeed=10,targetRatio=0,client='test.client'),
              dict(id='zero-rule',enabled=True,trackerDomains=['other.org'],minLeechers=0,
                   minUploadRate=10,maxUploadRate=100,simultaneousSeed=10,targetRatio=0,client='test.client')]
cfg['torrentRuleOverrides']={}
base=dict(trackers=['bt.example.org'],rule='tracker-default',leechers=5,threshold=3,
          selected=False,active=False,eligible=True,reason='Éligible')
state={'qbitConnected':True,'dryRun':True,'error':None,
       'torrents':[dict(base,mode='qbit',name='Torrent qBittorrent',infoHash=H,hash=H[:12],
                        progress=100,stopped=True,qbitState='stoppedUP',ratio=0.2)],
       'solo':{'count':1,'active':0,'items':[dict(base,mode='solo',name='Torrent JOAL uniquement',
                        infoHash=H2,hash=H2[:12],addedOn=1700000000)]},
       'rules':[], 'totals':{'tagged':1,'selected':0,'eligible':1,'blocked':0,'evaluated':1},
       'swarm':{},'cleanup':{}}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),args=['--no-sandbox'])
    page=browser.new_page(viewport={'width':1400,'height':950})
    errors=[]
    page.on('pageerror', lambda error: errors.append(str(error)))
    html=(STATIC/'index.html').read_text()
    for tag in ('<link rel="stylesheet" href="/assets/app.css">',
                '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>'):
        html=html.replace(tag,'')
    page.set_content(html)
    page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''({config,state}) => {
      window.__fixtures={'/api/auth/session':{authenticated:true,csrfToken:'token'},
                         '/api/config':config,'/api/state':state,'/api/clients':{clients:[]}};
      window.__saves=[];
      window.fetch=async (path,options={})=>{
        if(path==='/api/config' && options.method==='POST'){
          const saved=JSON.parse(options.body); window.__saves.push(saved);
          window.__fixtures[path]=saved;
          return new Response(JSON.stringify({success:true}),{headers:{'Content-Type':'application/json'}});
        }
        const value=window.__fixtures[path];
        return new Response(JSON.stringify(value||{error:'missing'}),
          {status:value?200:404,headers:{'Content-Type':'application/json'}});
      };
    }''',{'config':cfg,'state':state})
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.wait_for_function("document.querySelectorAll('#overview-items tr').length===2")
    # Overview rule is also clickable.
    # Lignes visées par leur nom : l'ordre par défaut des tableaux a changé.
    page.locator('#overview-items tr', has_text='Torrent qBittorrent').locator('.rule-choice').click()
    dlg=page.locator('.torrent-rule-dialog')
    assert dlg.is_visible()
    assert dlg.locator('option').count()==4
    assert '0 leechers' in dlg.inner_text()
    dlg.locator('select').select_option('zero-rule')
    dlg.get_by_role('button',name='Enregistrer').click()
    page.wait_for_function('window.__saves.length===1')
    saved=page.evaluate('window.__saves[0]')
    assert saved['torrentRuleOverrides']=={H:'zero-rule'},saved
    assert saved['rules'][1]['trackerDomains']==['other.org']
    # La boîte se ferme après la réponse de l'enregistrement (asynchrone).
    page.wait_for_function("!document.querySelector('.torrent-rule-dialog')")
    # Torrent table: zero-rule selected; restore automatic on first torrent.
    page.locator('.nav-link[data-view="torrents"]').click()
    page.locator('#torrent-items tr', has_text='Torrent qBittorrent').locator('.rule-choice').click()
    dlg=page.locator('.torrent-rule-dialog')
    assert dlg.locator('select').input_value()=='zero-rule'
    dlg.locator('select').select_option('')
    dlg.get_by_role('button',name='Enregistrer').click()
    page.wait_for_function('window.__saves.length===2')
    assert page.evaluate('window.__saves[1].torrentRuleOverrides')=={}
    # Assign solo independently, verify not touching qbit torrent.
    page.locator('#torrent-items tr', has_text='Torrent JOAL uniquement').locator('.rule-choice').click()
    dlg=page.locator('.torrent-rule-dialog')
    dlg.locator('select').select_option('zero-rule')
    dlg.get_by_role('button',name='Enregistrer').click()
    page.wait_for_function('window.__saves.length===3')
    assert page.evaluate('window.__saves[2].torrentRuleOverrides')=={H2:'zero-rule'}
    for box in page.locator('#torrent-items input[data-mode-hash]').all():
        box.check()
    page.locator('[data-batch-rule]:visible').click()
    dlg=page.locator('.torrent-rule-dialog')
    assert dlg.get_by_role('button',name='Enregistrer').is_disabled()
    dlg.locator('select').select_option(':presence')
    dlg.get_by_role('button',name='Enregistrer').click()
    page.wait_for_function('window.__saves.length===4')
    saved=page.evaluate('window.__saves[3]')
    preset=saved['rules'][-1]
    assert preset['trackerDomains']==[] and preset['minUploadRate']==preset['maxUploadRate']==0
    assert saved['torrentRuleOverrides']=={H:preset['id'], H2:preset['id']}
    assert not errors,errors
    print('Browser v1.17: automatic / manual rule pick per torrent, solo and qBittorrent OK')
    browser.close()
