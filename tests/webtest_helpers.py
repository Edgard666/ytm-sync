"""HTTP test helpers for session-based auth."""
import json
import urllib.request


def login(base, password):
    payload = json.dumps({'username': 'admin', 'password': password}).encode()
    request = urllib.request.Request(base + '/api/auth/login', payload,
                                     headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=5) as response:
        cookie = response.headers.get('Set-Cookie', '').split(';', 1)[0]
        assert cookie.startswith('joal_session=')
    request = urllib.request.Request(base + '/api/auth/session', headers={'Cookie': cookie})
    with urllib.request.urlopen(request, timeout=5) as response:
        csrf = json.load(response)['csrfToken']
    return cookie, csrf


def auth_headers(cookie, csrf):
    return {'Cookie': cookie, 'X-CSRF-Token': csrf}
