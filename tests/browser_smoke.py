"""Optional browser check: python3 tests/browser_smoke.py (requires playwright/chromium)."""
import json
import threading
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'app/static'
CONFIG = json.loads((ROOT / 'config.example.json').read_text())
ROWS = [
    dict(name=f'Film {i:03d}', hash=f'{i:040x}'[:12], infoHash=f'{i:040x}',
         rule='tracker-exemple', leechers=i, ratio=i / 10, addedOn=1700000000 + i * 86400,
         trackers=['tracker.exemple.org'], stopped=True, progress=100, selected=False,
         eligible=False, reason='En attente', qbitState='stoppedUP')
    for i in range(57)
]
ROWS[0]['upload'] = {'uploadedBytes': 536870912, 'sizeBytes': 1073741824, 'targetBytes': 1073741824, 'percent': 50}
ROWS[0]['joalSession'] = {'active': True, 'uptimeSeconds': 120, 'simulatedBytes': None}
STATE = {
    'dryRun': False, 'error': None, 'qbitConnected': True, 'rules': [],
    'totals': {'tagged': 57, 'selected': 0, 'eligible': 0, 'blocked': 57, 'evaluated': 57},
    'torrents': ROWS, 'swarm': {}, 'cleanup': {}
}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        paths = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/assets/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/assets/torrent_table.js': ('torrent_table.js', 'text/javascript; charset=utf-8'),
                 '/assets/app.css': ('app.css', 'text/css; charset=utf-8')}
        if self.path in paths:
            name, ctype = paths[self.path]
            body = (STATIC / name).read_bytes()
        else:
            payload = {'/api/config': CONFIG, '/api/state': STATE, '/api/clients': {'clients': []}, '/api/auth/session': {'authenticated': True, 'csrfToken': 'browser-test-csrf'}}.get(self.path)
            if payload is None:
                self.send_error(404)
                return
            body, ctype = json.dumps(payload).encode('utf8'), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return


def main():
    # Run entirely in an isolated browser document; no localhost/network access needed.
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM', '/usr/bin/chromium'), args=['--no-sandbox'])
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        html = (STATIC / 'index.html').read_text()
        html = html.replace('<link rel="stylesheet" href="/assets/app.css">', '')
        html = html.replace('<script src="/assets/torrent_table.js" defer></script>', '')
        html = html.replace('<script src="/assets/app.js" defer></script>', '')
        page.set_content(html)
        page.evaluate('''() => {
            const storage = new Map();
            Object.defineProperty(window, 'localStorage', { configurable: true, value: {
                getItem: (k) => storage.has(k) ? storage.get(k) : null,
                setItem: (k, v) => storage.set(k, String(v)),
                removeItem: (k) => storage.delete(k)
            }});
        }''')
        page.add_style_tag(content=(STATIC / 'app.css').read_text())
        config = json.loads(json.dumps(CONFIG))
        config['dryRun'] = False
        config['handoff']['enabled'] = True
        fixtures = {'/api/state': STATE, '/api/config': config, '/api/clients': {'clients': []}, '/api/auth/session': {'authenticated': True, 'csrfToken': 'browser-test-csrf'}}
        page.evaluate('''fixtures => {
            window.__mockFixtures = fixtures;
            window.__manualCalls = [];
            window.fetch = async (path, options) => {
                if (path === '/api/torrents/handoff') {
                    window.__manualCalls.push(JSON.parse(options.body));
                    return new Response(JSON.stringify({accepted:true,message:'Demande enregistree'}),
                        { status:202,headers:{'Content-Type':'application/json'}});
                }
                return new Response(JSON.stringify(window.__mockFixtures[path]),
                    { status: window.__mockFixtures[path] ? 200 : 404,
                      headers: { 'Content-Type': 'application/json' }});
            };
        }''', fixtures)
        page.add_script_tag(content=(STATIC / 'torrent_table.js').read_text())
        page.add_script_tag(content=(STATIC / 'app.js').read_text())
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")  # sections repliées depuis le design actuel
        page.locator('.nav-link[data-view="torrents"]').click()
        page.wait_for_function("document.querySelectorAll('#torrent-items tr').length === 57")
        assert page.locator('#torrent-items tr').first.locator('[role=progressbar]').get_attribute('aria-valuenow') == '50'
        # Libellé actuel quand aucune mesure JOAL n'est disponible.
        assert 'JOAL · Indisponible · Volume session : —' in page.locator('#torrent-items tr').first.inner_text()
        page.locator('#torrent-page-size').select_option('10')
        assert page.locator('#torrent-items tr').count() == 10
        assert 'Page 1 / 6' in page.locator('#torrent-page-label').inner_text()
        page.locator('#torrent-next').click()
        assert 'Film 010' in page.locator('#torrent-items tr').first.inner_text()
        page.locator('button[data-sort-key="leechers"]').click()
        assert 'Film 056' in page.locator('#torrent-items tr').first.inner_text()
        assert '↓' in page.locator('button[data-sort-key="leechers"]').inner_text()
        page.locator('button[data-sort-key="leechers"]').click()
        assert 'Film 000' in page.locator('#torrent-items tr').first.inner_text()
        page.locator('button[data-sort-key="addedOn"]').click()
        assert 'Film 056' in page.locator('#torrent-items tr').first.inner_text()
        assert page.locator('#torrent-items tr').first.locator('td').nth(6).inner_text() != '—'
        page.locator('#torrent-search').fill('Film 03')
        assert page.locator('#torrent-items tr').count() == 10
        assert '10 ligne(s)' in page.locator('#table-count').inner_text()
        page.locator('#torrent-page-size').select_option('20')
        assert page.locator('#torrent-page-label').inner_text() == 'Page 1 / 1'
        assert page.locator('#torrent-prev').is_disabled()
        page.locator('#torrent-search').fill('')
        page.locator('#torrent-page-size').select_option('500')
        assert page.locator('#torrent-items tr').count() == 57
        page.on('dialog', lambda dialog: dialog.accept())
        page.locator('#torrent-items tr').first.get_by_role('button', name='Vers JOAL').click()
        page.wait_for_function('window.__manualCalls.length === 1')
        assert page.evaluate('window.__manualCalls[0].target') == 'joal'
        assert page.evaluate('window.__manualCalls[0].infoHash') in {r['infoHash'] for r in ROWS}
        page.locator('#torrent-items tr').first.get_by_role('button', name='Vers qBittorrent').click()
        page.wait_for_function('window.__manualCalls.length === 2')
        assert page.evaluate('window.__manualCalls[1].target') == 'qbit'
        # v1.11.1: verify real width changes, scroll synchronization and reset.
        page.set_viewport_size({'width': 1050, 'height': 700})
        page.locator('.nav-link[data-view="torrents"]').click() if page.locator('#view-torrents').get_attribute('class') == 'view hidden' else None
        page.wait_for_function("document.querySelector('#torrent-table-scroll').scrollWidth > document.querySelector('#torrent-table-scroll').clientWidth")
        assert page.locator('#torrent-horizontal-nav').is_visible()
        w0 = page.locator('#torrent-table col').first.evaluate('(el) => parseInt(el.style.width)')
        first_handle = page.locator('#torrent-table .col-resizer').first
        first_handle.focus()
        first_handle.press('ArrowRight')
        w1 = page.locator('#torrent-table col').first.evaluate('(el) => parseInt(el.style.width)')
        assert w1 == w0 + 20, (w0, w1)
        saved = page.evaluate("JSON.parse(localStorage.getItem('joal-smart-torrent-widths-v1'))")
        assert saved[0] == w1
        # Drag the resize handle (not merely via keyboard) and persist on pointerup.
        grip = first_handle.bounding_box()
        assert grip
        page.mouse.move(grip['x'] + grip['width'] / 2, grip['y'] + grip['height'] / 2)
        page.mouse.down()
        page.mouse.move(grip['x'] + grip['width'] / 2 + 60, grip['y'] + grip['height'] / 2, steps=4)
        page.mouse.up()
        assert page.locator('#torrent-table col').first.evaluate('(el) => parseInt(el.style.width)') >= w1 + 40
        assert page.evaluate("JSON.parse(localStorage.getItem('joal-smart-torrent-widths-v1'))[0]") >= w1 + 40
        page.locator('#torrent-bottom-scroll').evaluate('(el) => el.scrollLeft = 400')
        page.wait_for_function("Math.abs(document.querySelector('#torrent-table-scroll').scrollLeft - 400) < 3")
        assert page.locator('#torrent-scroll-right').is_enabled()
        page.locator('#torrent-reset-widths').click()
        assert page.locator('#torrent-table col').first.evaluate('(el) => parseInt(el.style.width)') == w0
        assert page.evaluate("JSON.parse(localStorage.getItem('joal-smart-torrent-widths-v1'))[0]") == w0
        # The secondary bar stays in the viewport halfway through a long table.
        page.evaluate("window.scrollTo(0, Math.floor(document.querySelector('#torrent-table').getBoundingClientRect().height / 2))")
        nav_metrics = page.locator('#torrent-horizontal-nav').evaluate('(el) => ({ top: el.getBoundingClientRect().top, bottom: el.getBoundingClientRect().bottom, h: innerHeight })')
        assert 0 <= nav_metrics['top'] < nav_metrics['bottom'] <= nav_metrics['h'], nav_metrics
        assert not errors, errors
        print('Browser UI smoke test: OK (57 rows, sort, page sizes, resizable widths, scrollbar sync).')
        browser.close()


if __name__ == '__main__':
    main()
