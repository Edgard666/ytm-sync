"""Browser validation: exclusion tooltip and safe multi-tracker rule assignment."""
from pathlib import Path
import json
import os
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
RULE = {
    'id': 't-ru', 'enabled': True, 'trackerDomains': ['t-ru.org'], 'minLeechers': 4,
    'minUploadRate': 20, 'maxUploadRate': 100, 'simultaneousSeed': 5,
    'client': 'client.client', 'targetRatio': 0
}
CONFIG = json.loads((ROOT / 'config.example.json').read_text())
CONFIG['qbitTag'] = 'Joal'
CONFIG['rules'] = [RULE, {**RULE, 'id':'second', 'trackerDomains':['second.example.org'], 'minLeechers': 8}]
ROW = {
    'name': 'No Man Sky', 'hash': 'a'*12, 'infoHash': 'a'*40,
    'rule':'t-ru', 'ruleEnabled': True, 'leechers':-1, 'threshold':4,
    'ratio':0.25,'addedOn':1700000000,'stopped':True,'progress':100,
    'eligible':False,'selected':False,'reason':'Tracker inactif ou nombre de leechers inconnu',
    'qbitState':'stoppedUP',
    'trackers':['bt4.t-ru.org', 'new1.other.test','new2.other.test'],
    'trackerDetails':[
        {'domain':'bt4.t-ru.org','protocol':'https','status':1,'leechers':None},
        {'domain':'new1.other.test','protocol':'https','status':1,'leechers':None},
        {'domain':'new2.other.test','protocol':'http','status':2,'leechers':5},
        {'domain':'udp.sample.test','protocol':'udp','status':2,'leechers':10}
    ],
    'checks':[
        {'label':'Règle active','result':True,'detail':'t-ru'},
        {'label':'Tracker couvert par la règle','result':True,'detail':'bt4.t-ru.org'},
        {'label':'Nombre de leechers disponible','result':False,'detail':'Inconnu'},
        {'label':'Seuil de leechers atteint','result':None,'detail':'Inconnu / minimum 4'}
    ]
}
STATE = {'qbitConnected':True,'dryRun':True,'error':None,'totals':{
    'tagged':1,'selected':0,'eligible':0,'blocked':1,'evaluated':1},
    'torrents':[ROW], 'rules':[], 'swarm':{}, 'cleanup':{}, 'evaluationTime':'2026-11-17T12:00:00Z'}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'), args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':1100,'height':700})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    html = (STATIC/'index.html').read_text()
    html = html.replace('<link rel="stylesheet" href="/assets/app.css">', '')
    html = html.replace('<script src="/assets/torrent_table.js" defer></script>', '')
    html = html.replace('<script src="/assets/app.js" defer></script>', '')
    page.set_content(html)
    page.add_style_tag(content=(STATIC/'app.css').read_text())
    page.evaluate('''() => {
      const store = new Map();
      Object.defineProperty(window,'localStorage',{configurable:true,value:{
        getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,String(v))}});
    }''')
    page.evaluate('''({config,state}) => {
      window.__fixtures = {'/api/config':config,'/api/state':state,'/api/clients':{'clients':[]},'/api/auth/session':{'authenticated':true,'csrfToken':'browser-test-csrf'}};
      window.__saves = [];
      window.fetch = async (path,options={}) => {
        if(path==='/api/config' && options.method==='POST') {
          let saved=JSON.parse(options.body);
          window.__saves.push(saved);
          window.__fixtures[path]=saved;
          return new Response(JSON.stringify({ok:true}), {headers:{'Content-Type':'application/json'}});
        }
        let value=window.__fixtures[path];
        return new Response(JSON.stringify(value||{error:'Unknown'}),
          {status:value?200:404,headers:{'Content-Type':'application/json'}});
      };
    }''',{'config':CONFIG,'state':STATE})
    page.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    page.add_script_tag(content=(STATIC/'app.js').read_text())
    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    page.locator('.nav-link[data-view="torrents"]').click()
    page.wait_for_function("document.querySelectorAll('#torrent-items tr').length===1")
    # Condition-by-condition tooltip, hover and keyboard focus.
    badge = page.locator('#torrent-items .status-check-trigger')
    badge.hover()
    tooltip = page.locator('#status-inspector')
    assert tooltip.is_visible()
    assert 'Nombre de leechers disponible' in tooltip.inner_text()
    assert 'Inconnu / minimum 4' in tooltip.inner_text()
    assert tooltip.locator('.check-pass').count() == 2
    assert tooltip.locator('.check-fail').count() == 1
    assert tooltip.locator('.check-unknown').count() == 1
    # All tracker domains show (including UDP), and all uncovered domains are clickable.
    add_buttons=page.locator('#torrent-items .tracker-add')
    assert add_buttons.count() == 3, add_buttons.count()
    assert 'udp.sample.test' in page.locator('#torrent-items .tracker-list-cell').inner_text()
    page.locator('#torrent-items .tracker-add').first.click()
    modal=page.locator('.assign-trackers-dialog')
    assert modal.is_visible()
    assert modal.locator('input[type=checkbox]:checked').count() == 1
    # Adding multiple domains is still possible, but never preselects unrelated domains.
    modal.locator('input[type=checkbox][value="new2.other.test"]').check()
    assert 'SECRET' not in modal.inner_text()
    modal.get_by_role('button',name='Ajouter à la règle').click()
    page.wait_for_function('window.__saves.length === 1')
    saved=page.evaluate('window.__saves[0]')
    assert saved['rules'][0]['trackerDomains'] == ['t-ru.org','new1.other.test','new2.other.test']
    assert 'SECRET' not in json.dumps(saved)
    # Already-covered domain must remain clickable to attach it to a SECOND rule.
    page.locator('#torrent-items button.tracker-covered').first.click()
    another = page.locator('.assign-trackers-dialog')
    assert another.is_visible()
    another.locator('select').select_option('second')
    assert another.locator('input[type=checkbox][value="bt4.t-ru.org"]').is_checked()
    another.get_by_role('button',name='Ajouter à la règle').click()
    page.wait_for_function('window.__saves.length === 2')
    second_saved = page.evaluate('window.__saves[1]')
    assert 'bt4.t-ru.org' in second_saved['rules'][1]['trackerDomains']
    assert second_saved['rules'][0]['trackerDomains'] == ['t-ru.org', 'new1.other.test', 'new2.other.test']
    assert 'SECRET' not in json.dumps(second_saved)
    assert not errors, errors
    print('Browser: shared domain selected in two rules, no secret leaks: OK')
    browser.close()
