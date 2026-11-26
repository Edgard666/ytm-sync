"""Comparaison des titres : normalisation, versions, similarite, qualite, regles d'exclusion, suggestions."""
import re
import unicodedata

from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlparse


# ------------------------------------------------------------------ utilitaires
_SECRET_RX = [
    (re.compile(r"([?&](?:t|s|p|token|api_key|apikey|password)=)[^&\s'\"]+", re.I), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1***"),
    (re.compile(r"\bsk_[A-Za-z0-9_-]{6,}"), "sk_***"),
]


def redact(text):
    """Masque tout ce qui ressemble a un secret (jetons Subsonic, cles API) avant journal ou notification."""
    text = str(text)
    for rx, rep in _SECRET_RX:
        text = rx.sub(rep, text)
    return text


# Mots (entiers) qui, entre parentheses/crochets, ne font pas partie du titre
_NOISE_WORDS = (r"official|officiel|officielle|video|videoclip|audio|lyrics?|paroles|clip|hd|hq|4k|"
                r"remaster(?:ed)?|visuali[sz]er|live|version|edit|explicit|clean|from|ost|soundtrack|"
                r"bande originale|prod|mono|stereo|bonus track|single")
NOISE = re.compile(r"[\(\[][^\)\]]*\b(?:" + _NOISE_WORDS + r")\b[^\)\]]*[\)\]]", re.I)
# Suffixes apres un tiret : "Titre - Radio Edit", "Titre - 2011 Remaster", "Titre - Live at Wembley"
DASH_SUFFIX = re.compile(r"\s[-\u2013\u2014]\s[^-\u2013\u2014]*\b(?:remaster(?:ed)?|radio edit|edit|single version|"
                         r"album version|version|mono|stereo|live|remix|mix|acoustic|instrumental|demo|"
                         r"bonus track)\b[^-\u2013\u2014]*$", re.I)
FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring)\s.*$", re.I)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = NOISE.sub(" ", s)
    s = DASH_SUFFIX.sub("", s)
    s = FEAT.sub("", s)
    s = re.sub(r"\s-\s*topic$", "", s, flags=re.I)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def sim(a, b):
    return _title_sim(norm(a), norm(b))


VERSION_TAGS = {
    "live": re.compile(r"\b(live|en concert|en direct|in concert)\b"),
    "remix": re.compile(r"\b(remix|rmx|remixed|bootleg|rework|club mix|extended mix|dub mix)\b"),
    "acoustic": re.compile(r"\b(acoustic|acoustique|unplugged|acustico)\b"),
    "instrumental": re.compile(r"\b(instrumental|karaoke)\b"),
}


def version_tags(*texts):
    """Versions particulieres detectees dans un titre (et eventuellement un album)."""
    t = unicodedata.normalize("NFKD", " ".join(x or "" for x in texts)).encode("ascii", "ignore").decode().lower()
    return frozenset(k for k, rx in VERSION_TAGS.items() if rx.search(t))


def song_key(artist, title):
    """Identifiant d'un morceau independant de la video (clip / audio)."""
    return f"{norm(artist)}|{norm(title)}|{','.join(sorted(version_tags(title)))}"


def pl_id(x):
    x = (x or "").strip()
    if "list=" in x:
        x = parse_qs(urlparse(x).query).get("list", [x])[0]
    return x[2:] if x.startswith("VL") else x


# ------------------------------------------------------------------ correspondance
def _ratio(a, b):
    sm = SequenceMatcher(None, a, b)
    return sm.ratio() if sm.real_quick_ratio() > 0.3 else 0.0


def _title_sim(a, b):
    """Similarite de deux titres DEJA normalises. Pas de bonus d'inclusion :
    « hello » ne doit pas correspondre a « hello goodbye »."""
    if not a or not b:
        return 0.0
    return 1.0 if a == b else _ratio(a, b)


def _artist_sim(a, b):
    """Similarite de deux artistes DEJA normalises. Inclusion par mots entiers acceptee :
    « beatles » = « the beatles », « stromae » = « stromae angele » (collaborations)."""
    if not a or not b:
        return 0.0
    if a == b or f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        return 1.0
    return _ratio(a, b)


_sim_n = _artist_sim  # compatibilite


def best_match(cands, title, artist, t_th, a_th, album=None, duration=0, strict_versions=True, blocked=()):
    """Meilleur candidat Navidrome pour un titre YouTube, ou None.
    - versions : un titre Live/Remix/Acoustique/Instrumental ne correspond qu'a la meme version
    - duree    : departage les candidats (le plus proche gagne) ; jamais de rejet, car SoulSync
                 cherche par nom et retelechargerait la meme version"""
    want = version_tags(title, album) if album else version_tags(title)
    nt, na = norm(title), norm(artist)
    best, best_score, seen = None, -1.0, set()
    for s in cands:
        if s["id"] in seen or s["id"] in blocked:
            continue
        seen.add(s["id"])
        ts = _title_sim(nt, s.get("_nt") or norm(s.get("title")))
        if ts < t_th:
            continue
        ars = max(_artist_sim(na, s.get("_na") or norm(s.get("artist"))),
                  _artist_sim(na, s.get("_naa") or norm(s.get("albumArtist") or s.get("displayAlbumArtist"))))
        if ars < a_th:
            continue
        if strict_versions:
            got = version_tags(s.get("title"), s.get("album")) if album else version_tags(s.get("title"))
            if got != want:
                continue
        score = ts + ars + quality_rank(s) * 0.05   # a egalite, la meilleure qualite gagne
        if duration and s.get("duration"):
            score -= min(abs(duration - s["duration"]), 120) / 120
        if score > best_score:
            best, best_score = s, score
    return best


LOSSLESS = {"flac", "alac", "wav", "aiff", "aif", "ape", "wv", "dsf", "dff"}


def quality_rank(s):
    """0..1 : sans perte = 1, sinon proportionnel au debit (320 kbps ~ 0.9)."""
    if (s.get("suffix") or "").lower() in LOSSLESS:
        return 1.0
    return min((s.get("bitRate") or 0) / 355, 0.95)


def low_quality(s, min_kbps):
    """Texte « MP3 128 kbps » si le fichier est sous le seuil (jamais pour un format sans perte)."""
    if not min_kbps or (s.get("suffix") or "").lower() in LOSSLESS:
        return None
    br = s.get("bitRate") or 0
    return f"{(s.get('suffix') or '?').upper()} {br} kbps" if 0 < br < min_kbps else None


def parse_rules(text):
    """Regles d'exclusion : une par ligne. 'artiste: X', 'album: X', 'titre: X' ou juste un mot-cle (titre)."""
    rules = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        field, val = "title", line
        m = re.match(r"^(artiste|artist|album|titre|title)\s*:\s*(.+)$", line, re.I)
        if m:
            field = {"artiste": "artist", "artist": "artist", "album": "album"}.get(m.group(1).lower(), "title")
            val = m.group(2)
        # pas de norm() : il retirerait justement des mots comme "live" ou "remaster"
        v = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", unicodedata.normalize("NFKD", val)
                                       .encode("ascii", "ignore").decode().lower())).strip()
        if v:
            rules.append((field, v, line))
    return rules


def match_rule(rules, title, artist, album):
    def flat(x):
        x = unicodedata.normalize("NFKD", x or "").encode("ascii", "ignore").decode().lower()
        return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", x)).strip() + " "
    vals = {"title": flat(title), "artist": flat(artist), "album": flat(album)}
    for field, v, raw in rules:
        if f" {v} " in vals[field]:
            return raw
    return None


def suggest(songs, title, artist, duration=0, n=5):
    """Morceaux Navidrome les plus proches (sans seuil), avec un score en %, pour « Lier… »."""
    nt, na = norm(title), norm(artist)
    want = version_tags(title)
    scored = []
    for s in songs:
        st = s.get("_nt") or norm(s.get("title"))
        ts = _title_sim(nt, st)
        if nt and st and (f" {nt} " in f" {st} " or f" {st} " in f" {nt} "):
            ts = max(ts, 0.8)  # titre contenu mot pour mot : bon indice pour une suggestion (pas pour l'auto)
        if ts < 0.45:
            continue
        ars = max(_artist_sim(na, s.get("_na") or norm(s.get("artist"))),
                  _artist_sim(na, s.get("_naa") or norm(s.get("albumArtist"))))
        score = 0.6 * ts + 0.4 * ars
        if version_tags(s.get("title")) != want:
            score -= 0.1
        if duration and s.get("duration"):
            score -= min(abs(duration - s["duration"]), 120) / 120 * 0.1
        scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [dict({k: s.get(k) for k in ("id", "title", "artist", "album", "duration", "path")},
                 score=round(max(0.0, sc) * 100)) for sc, s in scored[:n]]
