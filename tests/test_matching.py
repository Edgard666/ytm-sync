"""Comparaison des titres : chaque cas ici correspond a un bug reel ou a un piege connu."""
import pytest

from app.engine import best_match, match_rule, norm, parse_rules, pl_id, song_key, version_tags

LIB = [
    {"id": "hello_goodbye", "title": "Hello, Goodbye", "artist": "The Beatles"},
    {"id": "love_me_do", "title": "Love Me Do", "artist": "The Beatles"},
    {"id": "help", "title": "Help!", "artist": "Beatles"},
    {"id": "alors", "title": "Alors on danse - Radio Edit", "artist": "Stromae", "duration": 207},
    {"id": "papa", "title": "Papaoutai", "artist": "Stromae, Angèle", "duration": 232},
    {"id": "letitgo", "title": "Let It Go", "artist": "Idina Menzel"},
    {"id": "bohemian", "title": "Bohemian Rhapsody - Remastered 2011", "artist": "Queen", "duration": 355},
    {"id": "bohemian_live", "title": "Bohemian Rhapsody (Live Aid)", "artist": "Queen", "duration": 360},
    {"id": "oliver_rmx", "title": "Oliver (Delivery Remix)", "artist": "DJ X"},
    {"id": "song_club", "title": "Song - Club Mix", "artist": "Y"},
]


def find(title, artist, **kw):
    hit = best_match(LIB, title, artist, 0.85, 0.6, **kw)
    return hit and hit["id"]


@pytest.mark.parametrize("title,artist,expected", [
    ("Hello", "The Beatles", None),                      # bug v3.0 : inclusion = correspondance
    ("Love", "The Beatles", None),
    ("Help!", "The Beatles", "help"),                    # « the » en plus dans l'artiste
    ("Alors On Danse (Official Video)", "Stromae", "alors"),
    ('Let It Go (From "Frozen")', "Idina Menzel", "letitgo"),
    ("Papaoutai", "Stromae - Topic", "papa"),            # suffixe Topic + collaboration
    ("Bohemian Rhapsody (Remastered 2011)", "Queen", "bohemian"),
    ("Bohemian Rhapsody (Live Aid 1985)", "Queen", "bohemian_live"),  # version live distinguee
    ("Oliver (Delivery Remix)", "DJ X", "oliver_rmx"),   # bug v3.0 : « delivery » contient « live »
    ("Oliver", "DJ X", None),                            # le remix n'est pas l'original
    ("Song", "Y", None),                                 # le club mix n'est pas l'original
    ("Papaoutai", "Inconnu", None),                      # mauvais artiste
])
def test_matching(title, artist, expected):
    assert find(title, artist) == expected


def test_blocked_and_duration():
    assert find("Bohemian Rhapsody", "Queen", blocked={"bohemian"}) is None
    two = [dict(LIB[6], id="a", duration=200), dict(LIB[6], id="b", duration=354)]
    assert best_match(two, "Bohemian Rhapsody", "Queen", 0.85, 0.6, duration=355)["id"] == "b"


def test_norm_and_tags():
    assert norm("Alors On Danse (Official Video) feat. X") == "alors on danse"
    assert norm("Oliver (Delivery Remix)") == "oliver delivery remix"
    assert version_tags("Song (Live)") == {"live"}
    assert version_tags("Delivery") == frozenset()
    assert song_key("Stromae - Topic", "Papaoutai (Clip officiel)") == song_key("Stromae", "Papaoutai")


def test_rules():
    rules = parse_rules("karaoke\nartiste: Nightcore\n# commentaire\nalbum: Live at Wembley")
    assert match_rule(rules, "Hello (Karaoke Version)", "Adele", None) == "karaoke"
    assert match_rule(rules, "X", "Nightcore Music", None) == "artiste: Nightcore"
    assert match_rule(rules, "Karaokes", "a", None) is None          # mot entier uniquement
    assert match_rule(rules, "X", "Queen", "Live at Wembley '86") == "album: Live at Wembley"


def test_pl_id():
    assert pl_id("https://music.youtube.com/playlist?list=PLabc&si=x") == "PLabc"
    assert pl_id("VLPLabc") == "PLabc"
    assert pl_id("LM") == "LM"


def test_suggestions():
    from app.engine import suggest
    res = suggest(LIB, "Hello", "The Beatles")
    assert res and res[0]["id"] == "hello_goodbye" and res[0]["score"] < 100  # propose en premier, sans certitude
    assert suggest(LIB, "Papaoutai", "Stromae")[0]["id"] == "papa"


def test_quality():
    from app.engine import low_quality, quality_rank
    assert low_quality({"suffix": "mp3", "bitRate": 128}, 192) == "MP3 128 kbps"
    assert low_quality({"suffix": "mp3", "bitRate": 320}, 192) is None
    assert low_quality({"suffix": "flac", "bitRate": 900}, 1000) is None        # sans perte : jamais signale
    assert low_quality({"suffix": "mp3", "bitRate": 128}, 0) is None             # option desactivee
    a = {"id": "a", "title": "Song", "artist": "X", "suffix": "mp3", "bitRate": 128}
    b = {"id": "b", "title": "Song", "artist": "X", "suffix": "flac", "bitRate": 1000}
    assert best_match([a, b], "Song", "X", 0.85, 0.6)["id"] == "b"             # a egalite : meilleure qualite
    assert quality_rank(b) > quality_rank(a)


def test_watch_playlist_mix():
    from app.engine import watch_as_playlist

    class YT:
        def get_watch_playlist(self, playlistId, limit):
            return {"playlist_title": "Supermix", "tracks": [{"videoId": "x", "title": "T", "length": "3:05"}, {"title": "sans id"}]}
    pl = watch_as_playlist(YT(), "RDTMAK123")
    assert pl["title"] == "Supermix" and len(pl["tracks"]) == 1 and pl["tracks"][0]["duration_seconds"] == 185
