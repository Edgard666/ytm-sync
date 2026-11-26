"""Choice of WebUI design, stored per installation in /data/interface.json.

The server points each HTML page at the chosen stylesheet before sending it,
so the page never flashes the other design while loading.
"""
import json
import os
import tempfile
from pathlib import Path

DEFAULT = 'classique'
DESIGNS = {
    'classique': {'label': 'Classique (sombre)', 'app': 'app.css', 'login': 'login.css', 'scheme': 'dark'},
    'baie': {'label': 'Baie (clair)', 'app': 'design-baie.css', 'login': 'design-baie-login.css', 'scheme': 'light'},
}

# Designs removed in v1.29 (Trame, Régie): a saved or restored choice falls back to Classique.
RETIRED = {'trame', 'trame-clair', 'trame-sombre', 'regie', 'regie-clair', 'regie-sombre'}


def _path(data):
    return Path(data) / 'interface.json'


def current(data):
    """Saved design, or the default when the file is absent or unreadable."""
    try:
        value = json.loads(_path(data).read_text(encoding='utf-8')).get('design')
    except (OSError, ValueError, AttributeError):
        return DEFAULT
    return value if value in DESIGNS else DEFAULT


def save(data, design):
    if design not in DESIGNS:
        raise ValueError('Design inconnu')
    target = _path(data)
    fd, temp = tempfile.mkstemp(dir=target.parent, prefix='.interface-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'design': design}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return design


def public(design):
    return {'design': design, 'stylesheet': '/assets/' + DESIGNS[design]['app'],
            'designs': [{'id': key, 'label': spec['label']} for key, spec in DESIGNS.items()]}


def render(html, page, design):
    """Rewrite the page's stylesheet link, design marker and colour scheme."""
    spec, default = DESIGNS.get(design, DESIGNS[DEFAULT]), DESIGNS[DEFAULT]
    html = html.replace(f'href="/assets/{default[page]}"'.encode(), f'href="/assets/{spec[page]}"'.encode(), 1)
    html = html.replace(b'<html lang="fr">', f'<html lang="fr" data-design="{design}">'.encode(), 1)
    return html.replace(b'<meta name="color-scheme" content="dark">',
                        f'<meta name="color-scheme" content="{spec["scheme"]}">'.encode(), 1)
