"""Offline Chromium smoke for login form styling and diagnostic dashboard rendering."""
import json
import os, tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=os.getenv('CHROMIUM','/usr/bin/chromium'),args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':1280,'height':800})
    failures = []
    page.on('pageerror',lambda error: failures.append(str(error)))
    login = (STATIC / 'login.html').read_text().replace('<link rel="stylesheet" href="/assets/login.css">','').replace('<script src="/assets/login.js" defer></script>','')
    page.set_content(login)
    page.add_style_tag(content=(STATIC / 'login.css').read_text())
    page.evaluate('''() => {
        window.__loginCalls = [];
        window.fetch = async (path, options={}) => {
            if (path === '/api/auth/session') return new Response(JSON.stringify({authenticated:false}),{status:200,headers:{'Content-Type':'application/json'}});
            window.__loginCalls.push({url:path, method:options.method, payload:JSON.parse(options.body)});
            return new Response(JSON.stringify({error:'incorrect'}),{status:401,headers:{'Content-Type':'application/json'}});
        };
    }''')
    page.add_script_tag(content=(STATIC / 'login.js').read_text())
    assert page.get_by_role('heading',name='Connexion').is_visible()
    page.locator('#login-password').fill('invalid-password')
    page.locator('#toggle-password').click()
    assert page.locator('#login-password').get_attribute('type') == 'text'
    page.locator('#toggle-password').click()
    assert page.locator('#login-password').get_attribute('type') == 'password'
    page.get_by_role('button',name='Se connecter').click()
    page.wait_for_function('window.__loginCalls.length === 1')
    assert page.locator('#login-error').is_visible()
    assert page.locator('#login-password').input_value() == ''
    page.locator('#login-error').evaluate('el=>el.hidden=true')
    page.screenshot(path=os.path.join(os.getenv('SCREENSHOT_DIR', tempfile.gettempdir()), 'joal-v112-login-preview.png'),full_page=True)
    # Dashboard mocks: no real network, no torrent announcements.
    dash = browser.new_page(viewport={'width':1280,'height':850})
    dash.on('pageerror',lambda e:failures.append(str(e)))
    content=(STATIC/'index.html').read_text()
    for src in ('<link rel="stylesheet" href="/assets/app.css">',
                '<script src="/assets/torrent_table.js" defer></script>',
                '<script src="/assets/app.js" defer></script>'):
        content=content.replace(src,'')
    dash.set_content(content)
    dash.add_style_tag(content=(STATIC/'app.css').read_text())
    config=json.loads((ROOT/'config.example.json').read_text())
    state={'dryRun': True, 'error':None, 'qbitConnected':True,
           'totals':{'tagged':1,'selected':0,'eligible':0,'blocked':1,'evaluated':1},
           'torrents':[{'name':'Torrent exemple','infoHash':'a'*40,'rule':'exemple', 'hash':'a'*40,
                        'leechers':0,'stopped':True,'progress':100,'selected':False,'eligible':False,'reason':'En attente'}],
           'rules':[], 'swarm':{}, 'cleanup':{}}
    history={'events':[{'time':'2026-11-17T14:40:00+00:00','code':'torrent.state','message':'Torrent : exclu → éligible',
                        'infoHash':'a'*40,'rule':'exemple','level':'info'}],
             'sessions':[{'startedAt':'2026-11-17T12:15:00+00:00','endedAt':'2026-11-17T13:30:00+00:00',
                          'infoHash':'a'*40,'rule':'exemple','status':'stopped','durationSeconds':4500,'simulatedBytes':None}],
             'joalTransferCounterAvailable':False}
    health={'qbitConnected':True,'activeProcesses':0,'inventoryAgeSeconds':5,'trackerMonitorLastAt':'2026-11-17T14:39:00+00:00'}
    fixtures={'/api/auth/session':{'authenticated':True,'csrfToken':'mock-csrf'},
              '/api/config':config,'/api/state':state,'/api/clients':{'clients':[]},
              '/api/health':health,'/api/diagnostics':history,'/api/diagnostics?infoHash='+'a'*40:history}
    dash.evaluate('''fixtures=> {
        const store=new Map();
        Object.defineProperty(window,'localStorage',{configurable:true,value:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,String(v))}});
        window.fetch=async (path, options={}) => {
            const data=fixtures[path];
            return new Response(JSON.stringify(data||{error:'not found'}),{status:data?200:404,headers:{'Content-Type':'application/json'}});
        }
    }''',fixtures)
    dash.add_script_tag(content=(STATIC/'torrent_table.js').read_text())
    dash.add_script_tag(content=(STATIC/'app.js').read_text())
    dash.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
    dash.locator('.nav-link[data-view="diagnostics"]').click()
    dash.wait_for_function("document.querySelector('#diag-events').textContent.includes('éligible')")
    assert 'Non mesuré' in dash.locator('#diag-sessions').inner_text()
    dash.screenshot(path=os.path.join(os.getenv('SCREENSHOT_DIR', tempfile.gettempdir()), 'joal-v112-diagnostics-preview.png'),full_page=True)
    dash.locator('.nav-link[data-view="torrents"]').click()
    dash.locator('#torrent-items .history-link').first.click()
    assert dash.locator('#torrent-history-dialog').is_visible()
    dash.wait_for_function("document.querySelector('#history-content').textContent.includes('Sessions JOAL')")
    assert 'Sessions JOAL' in dash.locator('#history-content').inner_text()
    dash.locator('#history-close').click()
    assert not dash.locator('#torrent-history-dialog').is_visible()
    assert not failures, failures
    print('Chromium: login layout/password/error, diagnostics, per-torrent history OK')
    browser.close()
