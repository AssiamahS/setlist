#!/usr/bin/env python3
"""Setlist — drop a 1001tracklists URL, get every track the DJ played,
download them through the amapiano library server.

1001tracklists sits behind Cloudflare Turnstile, so live fetches usually fail.
Resolution order per URL:
  1. direct fetch (works if Cloudflare ever lets us through)
  2. Wayback Machine latest snapshot (1001tracklists is heavily archived)
  3. manual: paste the page / bookmarklet ingest

Downloads are delegated to the amapiano server (localhost:8766) so tracks land
in the same library, Serato crates and rekordbox XML as everything else.
"""

import hashlib
import html as htmllib
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

PORT = 8787
AMAPIANO = "http://localhost:8766"
BASE = Path(__file__).parent
CACHE_DIR = BASE / "cache"
CACHE_DIR.mkdir(exist_ok=True)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Same Spotify app spotdl uses — read live config first, fall back to known pair
SPOTDL_CONFIG = Path.home() / ".spotdl" / "config.json"
SPOTIFY_ID = "5f573c9620494bae87890c0f08a60293"
SPOTIFY_SECRET = "212476d9b0f3472eaa762d90b19b0ba8"
try:
    _cfg = json.load(open(SPOTDL_CONFIG))
    SPOTIFY_ID = _cfg.get("client_id") or SPOTIFY_ID
    SPOTIFY_SECRET = _cfg.get("client_secret") or SPOTIFY_SECRET
except Exception:
    pass

app = Flask(__name__)


@app.after_request
def cors(resp):
    # bookmarklet POSTs from www.1001tracklists.com
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ── HTTP helpers ──

def http_get(url, timeout=30, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url, timeout=30, headers=None):
    return json.loads(http_get(url, timeout=timeout, headers=headers))


def cache_get(key, max_age=None):
    p = CACHE_DIR / f"{hashlib.md5(key.encode()).hexdigest()}.json"
    if not p.exists():
        return None
    if max_age and time.time() - p.stat().st_mtime > max_age:
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def cache_put(key, value):
    p = CACHE_DIR / f"{hashlib.md5(key.encode()).hexdigest()}.json"
    json.dump(value, open(p, "w"))


# ── 1001tracklists fetching ──

CHALLENGE_MARKER = "challenges.cloudflare.com/turnstile"


def is_challenge(html):
    return CHALLENGE_MARKER in html or "you will be forwarded" in html


def fetch_direct(url):
    try:
        html = http_get(url, timeout=20)
        if not is_challenge(html):
            return html, "live"
    except Exception:
        pass
    return None, None


def wayback_latest(url):
    """Latest Wayback snapshot timestamp for a URL, or None."""
    q = urllib.parse.quote(url, safe="")
    try:
        data = http_get_json(
            f"https://archive.org/wayback/available?url={q}&timestamp=99999999999999",
            timeout=30)
        snap = data.get("archived_snapshots", {}).get("closest")
        if snap and snap.get("available"):
            return snap["timestamp"]
    except Exception:
        pass
    return None


def fetch_wayback(url):
    ts = wayback_latest(url)
    if not ts:
        return None, None
    try:
        # id_ = original bytes, no wayback toolbar injected
        html = http_get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=60)
        return html, f"wayback {ts[:4]}-{ts[4:6]}-{ts[6:8]}"
    except Exception:
        return None, None


def fetch_page(url):
    """Fetch a 1001tracklists page: live first, Wayback fallback. Cached."""
    cached = cache_get("page:" + url, max_age=6 * 3600)
    if cached:
        return cached["html"], cached["source"]
    html, source = fetch_direct(url)
    if not html:
        html, source = fetch_wayback(url)
    if html:
        cache_put("page:" + url, {"html": html, "source": source})
    return html, source


# ── parsing ──

TRACK_RE = re.compile(
    r'itemtype="https?://schema\.org/MusicRecording">\s*'
    r'<meta itemprop="name" content="([^"]+)"')
TL_URL_RE = re.compile(
    r'1001tracklists\.com/tracklist/([a-z0-9]+)/([a-z0-9\-]+)\.html', re.I)
DJ_URL_RE = re.compile(r'1001tracklists\.com/dj/([a-z0-9\-]+)', re.I)


def parse_tracks(html):
    """Ordered unique track names from schema.org markup, split into artist/title."""
    seen, tracks = set(), []
    for raw in TRACK_RE.findall(html):
        name = htmllib.unescape(raw).strip()
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        tracks.append(make_track(name, len(tracks) + 1))
    return tracks


def make_track(name, n):
    artist, _, title = name.partition(" - ")
    if not title:
        artist, title = "", name
    artist, title = artist.strip(), title.strip()
    # "ID - ID", "Artist - ID", "ID2 - Title": unidentified — nothing to search for
    is_id = bool(re.fullmatch(r"ID\d*", artist, re.I) or re.fullmatch(r"ID\d*", title, re.I))
    return {"n": n, "name": name, "artist": artist, "title": title, "unknown": is_id}


# timestamp alternative MUST come first: ordered alternation otherwise lets
# `\d+[.):]` eat the "0:" out of "0:00" and the cue time is lost
CUE_PREFIX_RE = re.compile(r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?|[\d]+[.):]|[•*>▶︎♪-])*\s*")
TS_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])")


def ts_seconds(m):
    a, b, c = m.groups()
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


# description boilerplate that em-dash-normalizes into fake "Artist - Title" lines
JUNK_RE = re.compile(
    r"warning|epilepsy|strobe|subscribe|disclaimer|copyright|fair use|monetize|"
    r"follow me|linktr\.ee|patreon|discord|tickets|merch|filmed with|playlist tab", re.I)


def tracks_from_lines(lines):
    """'Artist - Title' tracks from free-form lines (descriptions, chapters,
    comments, pasted text). Keeps the cue timestamp (leading '23:10 Artist -
    Title' or trailing 'Artist - Title 23:10') as t seconds, strips
    numbering/bullets and normalizes en/em dashes."""
    tracks, seen = [], set()
    for line in lines:
        if JUNK_RE.search(line) or len(line) > 120:
            continue
        line = re.sub(r"\s+[–—]\s+", " - ", line)
        prefix = CUE_PREFIX_RE.match(line).group(0)
        m = TS_RE.search(prefix)
        line = line[len(prefix):].strip()
        if not m:
            tail = TS_RE.search(line[-10:])
            if tail and line.endswith(tail.group(0)):
                m = tail
                line = line[:-len(tail.group(0))].rstrip(" -–—([")
        if " - " not in line or len(line) < 6 or line.lower() in seen:
            continue
        if line.count("http") or line.count("@") > 1:
            continue  # link/social lines, not tracks
        seen.add(line.lower())
        track = make_track(line, len(tracks) + 1)
        if m:
            track["t"] = ts_seconds(m)
        tracks.append(track)
    return tracks


def parse_set_title(html):
    m = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if m:
        t = htmllib.unescape(m.group(1))
        if "1001Tracklists" not in t:
            return t
    m = re.search(r'itemtype="https?://schema\.org/MusicPlaylist"[^>]*>\s*'
                  r'<meta itemprop="name" content="([^"]+)"', html)
    if m:
        return htmllib.unescape(m.group(1))
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    return htmllib.unescape(m.group(1)).strip() if m else "Tracklist"


def parse_tracklist_links(html):
    """(url, slug) pairs for every tracklist link in a page."""
    out, seen = [], set()
    for m in re.finditer(r'href="(?:https?://(?:www\.)?1001tracklists\.com)?'
                         r'(/tracklist/([a-z0-9]+)/([a-z0-9\-]+)\.html)"', html, re.I):
        path, tlid, slug = m.groups()
        if tlid in seen:
            continue
        seen.add(tlid)
        out.append({"url": "https://www.1001tracklists.com" + path, "slug": slug})
    return out


def slug_to_title(slug):
    parts = slug.rsplit("-", 3)
    date = ""
    if len(parts) == 4 and all(p.isdigit() for p in parts[1:]):
        date = "-".join(parts[1:])
        slug = parts[0]
    return slug.replace("-", " ").title(), date


# ── YouTube sets + Apple Music DJ-mix albums ──

YT_URL_RE = re.compile(r"(?:youtube\.com/(?:watch|live/|shorts/)|youtu\.be/)", re.I)
APPLE_ALBUM_RE = re.compile(r"music\.apple\.com/[a-z]{2}/album/[^\s\"'<>)]+", re.I)
ISO_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def iso_duration_s(s):
    m = ISO_DUR_RE.fullmatch(s or "")
    if not m:
        return 0
    h, mn, sec = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + sec


def apple_album_tracks(album_url):
    """(album_title, tracks) from an Apple Music album page's JSON-LD.
    DJ mixes list every track played — Apple's own 1001tracklists."""
    if not album_url.startswith("http"):
        album_url = "https://" + album_url
    cached = cache_get("am:" + album_url, max_age=24 * 3600)
    if cached:
        return cached["title"], cached["tracks"]
    html = http_get(album_url, timeout=30)
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None, []
    d = json.loads(m.group(1))
    if d.get("@type") != "MusicAlbum":
        return None, []
    album_artist = (d.get("byArtist") or [{}])[0].get("name", "")
    title = d.get("name", "Apple Music album")
    tracks = []
    for t in d.get("tracks", []):
        name = t.get("name", "")
        name = re.sub(r"\s*[\[(](?:Mixed|from [^)\]]*)[)\]]", "", name).strip()
        if not name:
            continue
        if " - " not in name and album_artist:
            name = f"{album_artist} - {name}"
        track = make_track(name, len(tracks) + 1)
        track["duration_s"] = iso_duration_s(t.get("duration"))
        tracks.append(track)
    if any(t["duration_s"] for t in tracks):
        at = 0
        for track in tracks:  # cumulative mix position from segment durations
            track["t"] = at
            at += track["duration_s"]
    if tracks:
        cache_put("am:" + album_url, {"title": title, "tracks": tracks})
    return title, tracks


def ytdlp_json(url, comments=False):
    cmd = ["yt-dlp", "-J", "--no-playlist", url]
    if comments:
        cmd[1:1] = ["--write-comments",
                    "--extractor-args", "youtube:comment_sort=top;max_comments=40,40,0,0"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().split("\n")[-1][:200] if r.stderr else "yt-dlp failed")
    return json.loads(r.stdout)


def resolve_youtube(url):
    """Tracklist for a YouTube DJ set: chapters → description lines →
    Apple Music album linked in the description → top comments."""
    j = ytdlp_json(url)
    title = j.get("title") or "YouTube set"
    desc = j.get("description") or ""

    tracks = []
    for c in j.get("chapters") or []:
        line = re.sub(r"\s+[–—]\s+", " - ", c.get("title", ""))
        line = CUE_PREFIX_RE.sub("", line).strip()
        if " - " not in line or len(line) < 6:
            continue
        track = make_track(line, len(tracks) + 1)
        track["t"] = int(c.get("start_time") or 0)
        tracks.append(track)
    source = "youtube chapters"
    if len(tracks) < 3:
        tracks, source = tracks_from_lines(desc.splitlines()), "youtube description"
    if len(tracks) < 3:
        am = APPLE_ALBUM_RE.search(desc)
        if am:
            am_title, tracks = apple_album_tracks(am.group(0).rstrip(").,"))
            source = "apple music mix album"
    tl_url = None
    if len(tracks) < 3:
        tl_source, tl_tracks, tl_url = tl_lookup_by_title(title)
        if tl_tracks:
            tracks, source = tl_tracks, tl_source
    if len(tracks) < 3:
        try:
            jc = ytdlp_json(url, comments=True)
            for c in jc.get("comments") or []:
                found = tracks_from_lines((c.get("text") or "").splitlines())
                if len(found) >= 3:
                    tracks, source = found, "youtube comments"
                    break
        except Exception as e:
            print(f"[youtube] comments fetch failed: {e}", flush=True)
    return {"type": "tracklist", "url": url, "source": source,
            "title": title, "tracks": tracks, "tl_url": tl_url}


# ── find a set's 1001tracklists page by searching its title ──
# Fan uploads often have no tracklist while 1001TL has the full moderated one.

SEARCH_ENGINES = [
    "https://html.duckduckgo.com/html/?q={q}",
    "https://www.bing.com/search?q={q}",
    "https://www.mojeek.com/search?q={q}",
]
SLUG_STOPWORDS = {"the", "dj", "set", "live", "mix", "at", "and", "of", "in", "for"}
TITLE_NOISE_RE = re.compile(
    r"[\[(][^\])]*[\])]|full set|60fps|4k\b|hd\b|live stream|official|debut", re.I)


def search_tracklist_urls(query):
    """1001tracklists tracklist URLs for a free-text query, via whichever HTML
    search engine isn't currently challenge-walling us. Only hits are cached —
    a miss is usually an engine block, not proof the set isn't listed."""
    key = "tlsearch:" + query.lower()
    cached = cache_get(key, max_age=7 * 24 * 3600)
    if cached is not None:
        return cached
    q = urllib.parse.quote(f"site:1001tracklists.com {query}")
    found = []
    for tpl in SEARCH_ENGINES:
        try:
            page = http_get(tpl.format(q=q), timeout=20)
        except Exception:
            continue
        for chunk in (urllib.parse.unquote(page), page):
            for m in TL_URL_RE.finditer(chunk):
                tlid, slug = m.groups()
                if all(f["id"] != tlid for f in found):
                    found.append({
                        "id": tlid, "slug": slug,
                        "url": f"https://www.1001tracklists.com/tracklist/{tlid}/{slug}.html"})
        if found:
            break
        time.sleep(1)
    if found:
        cache_put(key, found)
    return found


def tl_lookup_by_title(title):
    """(source, tracks, found_url) for a set found on 1001tracklists by title
    search. found_url is set even when the page itself couldn't be fetched
    (Turnstile + not archived) so the UI can offer the bookmarklet route."""
    q = TITLE_NOISE_RE.sub(" ", title)
    words = re.sub(r"[^\w\s&'-]", " ", q).split()[:8]
    if len(words) < 2:
        return None, [], None
    title_tokens = set(re.findall(r"[a-z0-9]+", " ".join(words).lower())) - SLUG_STOPWORDS
    found_url = None
    for hit in search_tracklist_urls(" ".join(words))[:3]:
        slug_tokens = set(hit["slug"].split("-")) - SLUG_STOPWORDS
        if len(title_tokens & slug_tokens) < 2:
            continue  # different set entirely
        found_url = found_url or hit["url"]
        html, source = fetch_page(hit["url"])
        if not html:
            continue
        tracks = parse_tracks(html)
        if len(tracks) >= 3:
            return f"1001tracklists ({source})", tracks, hit["url"]
    return None, [], found_url


# ── DJ set discovery: DuckDuckGo (fresh) + archived DJ page (history) ──

def ddg_sets_for(dj_name):
    """Recent tracklist URLs for a DJ from DuckDuckGo's HTML endpoint."""
    cached = cache_get("ddg:" + dj_name.lower(), max_age=24 * 3600)
    if cached is not None:
        return cached
    sets, seen = [], set()
    for offset in (0, 30):
        q = urllib.parse.urlencode({
            "q": f'site:1001tracklists.com/tracklist "{dj_name}"', "s": offset})
        try:
            page = http_get(f"https://html.duckduckgo.com/html/?{q}", timeout=30)
        except Exception as e:
            print(f"[ddg] search failed: {e}", flush=True)
            break
        # results come both uddg-urlencoded and plain
        for chunk in (urllib.parse.unquote(page), page):
            for m in TL_URL_RE.finditer(chunk):
                tlid, slug = m.groups()
                if tlid in seen:
                    continue
                seen.add(tlid)
                title, date = slug_to_title(slug)
                sets.append({
                    "url": f"https://www.1001tracklists.com/tracklist/{tlid}/{slug}.html",
                    "id": tlid, "title": title, "date": date, "archived": "",
                })
        if "result__a" not in page:  # no (more) results
            break
        time.sleep(1)
    cache_put("ddg:" + dj_name.lower(), sets)
    return sets


def dj_display_name(dj_slug):
    """DJ display name from their (possibly old) archived DJ page title."""
    html, _ = fetch_page(f"https://www.1001tracklists.com/dj/{dj_slug}/index.html")
    if html:
        m = re.search(r"<title>([^<]+?)\s*(?:Tracklists|&sdot;|⋅)", html)
        if m and "1001Tracklists" not in m.group(1):
            return m.group(1).strip()
    return dj_slug.replace("-", " ").title()


# ── Spotify matching ──

_sp_token = {"token": None, "expires": 0}
_sp_lock = threading.Lock()


def spotify_token():
    with _sp_lock:
        if _sp_token["token"] and time.time() < _sp_token["expires"]:
            return _sp_token["token"]
        import base64
        auth = base64.b64encode(f"{SPOTIFY_ID}:{SPOTIFY_SECRET}".encode()).decode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        _sp_token["token"] = resp["access_token"]
        _sp_token["expires"] = time.time() + resp["expires_in"] - 60
        return _sp_token["token"]


def norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s.lower())
    s = re.sub(r"\b(ft|feat|featuring|vs|&|x|and|with)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def similarity(a, b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


MATCH_THRESHOLD = 0.55


def score_pair(artist, title, cand_artist, cand_title):
    return 0.55 * similarity(title, cand_title) + 0.45 * similarity(artist, cand_artist)


def spotify_api_search(artist, title):
    """Spotify search API. The shared spotdl app is often 429'd for the whole day,
    so treat any failure as 'try another way', not an error."""
    tok = spotify_token()
    best, best_score = None, 0.0
    q = urllib.parse.quote(f"{artist} {re.sub(r'[(].*?[)]', '', title).strip()}")
    data = http_get_json(
        f"https://api.spotify.com/v1/search?type=track&limit=5&q={q}",
        headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    for it in data.get("tracks", {}).get("items", []):
        sp_artists = ", ".join(a["name"] for a in it["artists"])
        score = score_pair(artist, title, sp_artists, it["name"])
        if score > best_score:
            best_score = score
            best = {
                "url": it["external_urls"]["spotify"],
                "artist": sp_artists,
                "title": it["name"],
                "duration_s": it["duration_ms"] // 1000,
                "score": round(score, 2),
            }
    return best if best_score >= MATCH_THRESHOLD else False  # False = definitive miss


def spotify_match(artist, title):
    """Best Spotify track, or None. The shared spotdl app quota is often burned
    for the day (429 Retry-After 86400) — treat that as 'no Spotify today'."""
    key = f"sp:{artist}|{title}".lower()
    cached = cache_get(key)
    if cached is not None:
        return cached or None
    try:
        result = spotify_api_search(artist, title)
    except Exception as e:
        print(f"[spotify] unavailable for {artist} - {title}: {e}", flush=True)
        return None  # transient — retry next time
    cache_put(key, result or False)
    return result or None


def itunes_match(artist, title):
    """Canonical artist/title/duration from the open iTunes Search API."""
    key = f"it:{artist}|{title}".lower()
    cached = cache_get(key)
    if cached is not None:
        return cached or None
    q = urllib.parse.urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 5})
    try:
        data = json.loads(http_get(f"https://itunes.apple.com/search?{q}", timeout=20))
    except Exception as e:
        print(f"[itunes] failed for {artist} - {title}: {e}", flush=True)
        return None
    best, best_score = None, 0.0
    for it in data.get("results", []):
        score = score_pair(artist, title, it.get("artistName", ""), it.get("trackName", ""))
        # prefer original releases over DJ-mix segments (segment durations lie)
        if "(mixed)" in it.get("trackName", "").lower() and "(mixed)" not in title.lower():
            score -= 0.15
        if score > best_score:
            best_score = score
            best = {"artist": it.get("artistName", ""), "title": it.get("trackName", ""),
                    "duration_s": (it.get("trackTimeMillis") or 0) // 1000,
                    "score": round(score, 2)}
    result = best if best_score >= MATCH_THRESHOLD else False
    cache_put(key, result)
    return result or None


def youtube_pick(artist, title, expected_s=0):
    """Pick the best real YouTube watch URL for a track. Prefers auto-generated
    'Topic' uploads (correct artist/track tags) and verifies duration when known."""
    key = f"yt:{artist}|{title}|{expected_s}".lower()
    cached = cache_get(key)
    if cached is not None:
        return cached or None
    fmt = "%(webpage_url)s\t%(artist,creator|)s\t%(track|)s\t%(duration|0)s\t%(channel|)s\t%(title|)s"
    try:
        r = subprocess.run(
            ["yt-dlp", "--print", fmt, "--no-playlist", "--flat-playlist",
             f"ytsearch3:{artist} {title} audio"],
            capture_output=True, text=True, timeout=60)
        lines = [l for l in r.stdout.strip().split("\n") if l.startswith("http")]
    except Exception:
        lines = []
    best, best_score = None, -1.0
    for line in lines:
        parts = (line.split("\t") + [""] * 6)[:6]
        url, yt_artist, yt_track, dur, channel, vtitle = parts
        try:
            dur = float(dur or 0)
        except ValueError:
            dur = 0
        if expected_s and dur and (dur > expected_s * 1.6 or dur < expected_s * 0.5):
            continue  # wrong thing: podcast, full set, snippet
        cand = f"{yt_artist} {yt_track}".strip() or vtitle or channel
        score = similarity(f"{artist} {title}", cand)
        if channel.endswith(" - Topic") or (yt_artist and yt_track):
            score += 0.3  # auto-generated upload → clean ID3 tags downstream
        if expected_s and dur:
            score += 0.2 - min(abs(dur - expected_s) / expected_s, 0.2)
        if score > best_score:
            best_score, best = score, url
    cache_put(key, best or False)
    return best


# ── Shazam auto-tagging: sample the mix every N sec, fingerprint each clip ──

# shazamio-core segfaults on this venv's Python 3.14 — recognition runs through
# cratemate's 3.13 venv, which has a working shazamio.
SHAZAM_PY = Path.home() / "cratemate" / ".venv" / "bin" / "python"
AUDIO_DIR = CACHE_DIR / "audio"
YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")

_shz_jobs = {}
_shz_lock = threading.Lock()


def yt_cache_key(url):
    m = YT_ID_RE.search(url)
    return "shazam:" + (m.group(1) if m else url)


def merge_shazam_samples(samples, stride):
    """Collapse per-sample recognitions into a tracklist: consecutive samples
    Shazam tags as the same song become one track starting at the first
    matching sample's offset. Unrecognized samples don't break a run —
    transitions and heavy drops often miss."""
    tracks, last_key = [], None
    for s in samples:
        artist, title = s.get("artist", ""), s.get("title", "")
        if not title:
            continue
        key = norm(f"{artist} {title}")
        if key == last_key:
            continue
        last_key = key
        track = make_track(f"{artist} - {title}" if artist else title, len(tracks) + 1)
        track["t"] = s["t"]
        tracks.append(track)
    return tracks


def _dl_mix_audio(url):
    """Download (or reuse) the mix's audio, named by video id."""
    AUDIO_DIR.mkdir(exist_ok=True)
    m = YT_ID_RE.search(url)
    stem = m.group(1) if m else hashlib.md5(url.encode()).hexdigest()[:12]
    existing = list(AUDIO_DIR.glob(stem + ".*"))
    if existing:
        return existing[0]
    cmd = ["yt-dlp", "-f", "bestaudio/best", "-o", str(AUDIO_DIR / f"{stem}.%(ext)s"),
           "--no-playlist", url]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 and re.search(r"instagram|tiktok|facebook", url, re.I):
        # login-walled platforms: retry with the user's browser session
        r = subprocess.run(cmd[:1] + ["--cookies-from-browser", "chrome"] + cmd[1:],
                           capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "yt-dlp failed").strip().split("\n")[-1][:200])
    existing = list(AUDIO_DIR.glob(stem + ".*"))
    if not existing:
        raise RuntimeError("yt-dlp reported success but no audio file landed")
    return existing[0]


def _run_shazam(job_id):
    with _shz_lock:
        job = _shz_jobs[job_id]
    url, stride = job["url"], job["stride"]
    try:
        if not SHAZAM_PY.exists():
            raise RuntimeError(f"no shazamio Python at {SHAZAM_PY}")
        try:
            r = subprocess.run(["yt-dlp", "--print", "title", "--no-playlist", url],
                               capture_output=True, text=True, timeout=60)
            job["title"] = r.stdout.strip().split("\n")[0] or job["title"]
        except Exception:
            pass
        job["stage"] = "downloading mix audio"
        audio = _dl_mix_audio(url)
        job["stage"] = "tagging"
        samples = []
        errlog = open(AUDIO_DIR / f"{job_id}.log", "w")
        proc = subprocess.Popen(
            [str(SHAZAM_PY), str(BASE / "shazam_tag.py"), str(audio), str(stride)],
            stdout=subprocess.PIPE, stderr=errlog, text=True)
        for line in proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("done"):
                break
            samples.append(d)
            job["sample"], job["samples"] = d["i"], d["total"]
            job["tracks"] = merge_shazam_samples(samples, stride)
        proc.wait(timeout=30)
        errlog.close()
        if not samples:
            raise RuntimeError(f"tagger produced no samples — see {errlog.name}")
        cache_put(yt_cache_key(url), {
            "type": "tracklist", "url": url, "source": "shazam",
            "title": job["title"], "tracks": job["tracks"]})
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]


@app.route("/api/shazam", methods=["POST"])
def shazam_start():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url.startswith("http"):
        return jsonify({"error": "a URL yt-dlp can fetch is required"}), 400
    cached = cache_get(yt_cache_key(url))
    if cached and not data.get("force"):
        return jsonify({"cached": True, **cached})
    job_id = hashlib.md5(f"shz{url}{time.time()}".encode()).hexdigest()[:10]
    job = {"id": job_id, "url": url, "stride": int(data.get("stride") or 45),
           "status": "running", "stage": "starting", "title": "YouTube mix",
           "sample": 0, "samples": 0, "tracks": []}
    with _shz_lock:
        _shz_jobs[job_id] = job
    threading.Thread(target=_run_shazam, args=(job_id,), daemon=True).start()
    return jsonify({"id": job_id})


@app.route("/api/shazam/<job_id>")
def shazam_status(job_id):
    with _shz_lock:
        job = _shz_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)


# ── Spotify playlist export (user OAuth through the shared spotdl app) ──
# The spotdl app whitelists http://127.0.0.1:8800/ (its user-auth port), so we
# run our one-shot OAuth listener there.

SPOTIFY_TOKEN_FILE = BASE / "spotify_token.json"
SPOTIFY_REDIRECT = "http://127.0.0.1:8800/"
SPOTIFY_SCOPES = "playlist-modify-private playlist-modify-public"
_sp_user = {"error": None}


def _spotify_token_request(params):
    import base64
    auth = base64.b64encode(f"{SPOTIFY_ID}:{SPOTIFY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _spotify_exchange(code):
    resp = _spotify_token_request({
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": SPOTIFY_REDIRECT})
    resp["expires_at"] = time.time() + resp.get("expires_in", 3600)
    json.dump(resp, open(SPOTIFY_TOKEN_FILE, "w"))


def sp_user_token():
    """Valid user access token, refreshing when stale, or None."""
    try:
        tok = json.load(open(SPOTIFY_TOKEN_FILE))
    except Exception:
        return None
    if time.time() < tok.get("expires_at", 0) - 30:
        return tok["access_token"]
    try:
        resp = _spotify_token_request({
            "grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
    except Exception as e:
        print(f"[spotify] refresh failed: {e}", flush=True)
        return None
    tok["access_token"] = resp["access_token"]
    tok["expires_at"] = time.time() + resp.get("expires_in", 3600)
    tok["refresh_token"] = resp.get("refresh_token") or tok["refresh_token"]
    json.dump(tok, open(SPOTIFY_TOKEN_FILE, "w"))
    return tok["access_token"]


def sp_api(method, path, token, body=None):
    req = urllib.request.Request(
        "https://api.spotify.com/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method=method)
    return json.loads(urllib.request.urlopen(req, timeout=20).read() or b"{}")


def _spotify_auth_listener(state):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [None])[0]
            ok = bool(code) and (qs.get("state") or [None])[0] == state
            if ok:
                try:
                    _spotify_exchange(code)
                    msg = "Spotify connected — close this tab and go back to Setlist."
                except Exception as e:
                    _sp_user["error"] = str(e)[:200]
                    msg = f"Spotify auth failed: {_sp_user['error']}"
            else:
                msg = "Spotify auth failed (denied or bad state)."
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(("<body style='background:#0c0e12;color:#e8eaee;"
                              "font:16px -apple-system;padding:40px'>"
                              + msg + "</body>").encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *a):
            pass

    try:
        HTTPServer(("127.0.0.1", 8800), Handler).serve_forever()
    except OSError as e:
        _sp_user["error"] = f"port 8800 busy: {e}"


@app.route("/api/spotify/status")
def spotify_status():
    tok = sp_user_token()
    if not tok:
        err, _sp_user["error"] = _sp_user["error"], None
        return jsonify({"connected": False, "error": err})
    try:
        me = sp_api("GET", "/me", tok)
        return jsonify({"connected": True, "user": me.get("display_name") or me.get("id")})
    except Exception as e:
        return jsonify({"connected": False, "error": str(e)[:120]})


@app.route("/api/spotify/connect", methods=["POST"])
def spotify_connect():
    state = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:12]
    _sp_user["error"] = None
    threading.Thread(target=_spotify_auth_listener, args=(state,), daemon=True).start()
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": SPOTIFY_ID, "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT, "scope": SPOTIFY_SCOPES, "state": state})
    return jsonify({"auth_url": url})


@app.route("/api/spotify/playlist", methods=["POST"])
def spotify_playlist():
    data = request.json or {}
    name = (data.get("name") or "Setlist").strip()
    items = [t for t in data.get("tracks", []) if not t.get("unknown") and not t.get("skip")]
    if not items:
        return jsonify({"error": "no tracks"}), 400
    tok = sp_user_token()
    if not tok:
        return jsonify({"error": "not connected", "need_auth": True}), 401
    uris, missed = [], []
    for t in items:
        m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)",
                      (t.get("match") or {}).get("url") or "")
        if m:
            uris.append("spotify:track:" + m.group(1))
            continue
        try:  # not matched at resolve time — search on the user token
            q = urllib.parse.quote(f"{t.get('artist', '')} {t.get('title', '')}".strip())
            res = sp_api("GET", f"/search?type=track&limit=3&q={q}", tok)
            best, best_score = None, 0.0
            for it in res.get("tracks", {}).get("items", []):
                s = score_pair(t.get("artist", ""), t.get("title", ""),
                               ", ".join(a["name"] for a in it["artists"]), it["name"])
                if s > best_score:
                    best, best_score = it, s
            if best and best_score >= MATCH_THRESHOLD:
                uris.append(best["uri"])
            else:
                missed.append(t.get("name") or t.get("title") or "?")
        except Exception:
            missed.append(t.get("name") or t.get("title") or "?")
    if not uris:
        return jsonify({"error": "no Spotify matches to add", "missed": missed}), 422
    me = sp_api("GET", "/me", tok)
    pl = sp_api("POST", f"/users/{me['id']}/playlists", tok,
                {"name": name, "public": False, "description": "made with setlist"})
    for i in range(0, len(uris), 100):
        sp_api("POST", f"/playlists/{pl['id']}/tracks", tok, {"uris": uris[i:i + 100]})
    return jsonify({"url": pl["external_urls"]["spotify"],
                    "added": len(uris), "missed": missed})


# ── download jobs (feed amapiano, limited concurrency) ──

_jobs = {}
_jobs_lock = threading.Lock()


def amapiano_up():
    try:
        http_get_json(f"{AMAPIANO}/api/downloads", timeout=3)
        return True
    except Exception:
        return False


def amapiano_download(url, name, meta_name=None):
    body = {"url": url, "name": name}
    if meta_name:
        body["meta_name"] = meta_name  # forces clean "Artist - Title" filename
    req = urllib.request.Request(
        f"{AMAPIANO}/api/download",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def amapiano_status(dl_id):
    return http_get_json(f"{AMAPIANO}/api/download/{dl_id}", timeout=10)


def export_serato_crate(name):
    """Ask amapiano to write a Serato .crate for the playlist it built while
    downloading this set (playlist name == set name)."""
    try:
        pls = http_get_json(f"{AMAPIANO}/api/playlists", timeout=10).get("playlists", [])
        pid = next((p["id"] for p in pls if p["name"] == name), None)
        if not pid:
            return {"error": f"amapiano has no playlist named {name!r}"}
        req = urllib.request.Request(
            f"{AMAPIANO}/api/serato/export",
            data=json.dumps({"playlist_id": pid}).encode(),
            headers={"Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
        return {"path": r.get("path"), "tracks": r.get("tracks")}
    except Exception as e:
        return {"error": str(e)[:200]}


def _run_job(job_id):
    with _jobs_lock:
        job = _jobs[job_id]
    name = job["name"]
    for item in job["items"]:
        if item.get("skip"):
            item["status"] = "skipped"
            continue
        url = item.get("spotify_url")
        source = "spotify"
        if not url:
            url = youtube_pick(item["artist"], item["title"], item.get("duration_s") or 0)
            source = "youtube"
        if not url:
            item["status"] = "not_found"
            continue
        item["source"] = source
        try:
            meta = None
            if source == "youtube":
                meta = f"{item['artist']} - {item['title']}" if item["artist"] else item["name"]
            resp = amapiano_download(url, name, meta)
            item["amapiano_id"] = resp.get("id")
            item["status"] = "downloading"
        except Exception as e:
            item["status"] = "error"
            item["error"] = str(e)[:200]
            continue
        # wait for this track before submitting the next — keeps yt-dlp serial
        deadline = time.time() + 300
        while time.time() < deadline:
            time.sleep(3)
            try:
                st = amapiano_status(item["amapiano_id"])
            except Exception:
                continue
            if st.get("status") in ("done", "error"):
                ok = st.get("status") == "done" and not st.get("error")
                item["status"] = "done" if ok else "error"
                if st.get("error"):
                    item["error"] = str(st["error"])[:200]
                break
        else:
            item["status"] = "timeout"
    if any(i["status"] == "done" for i in job["items"]):
        job["crate"] = export_serato_crate(name)
    with _jobs_lock:
        job["status"] = "done"
        job["finished"] = time.time()


# ── API ──

@app.route("/")
def index():
    return send_from_directory(BASE / "public", "index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "amapiano": amapiano_up()})


@app.route("/api/resolve", methods=["POST", "OPTIONS"])
def resolve():
    if request.method == "OPTIONS":
        return "", 204
    q = (request.json or {}).get("url", "").strip()
    if not q:
        return jsonify({"error": "URL or DJ name required"}), 400

    tl = TL_URL_RE.search(q)
    if tl:
        tlid, slug = tl.groups()
        url = f"https://www.1001tracklists.com/tracklist/{tlid}/{slug}.html"
        html, source = fetch_page(url)
        if not html:
            return jsonify({
                "type": "manual_needed", "url": url,
                "reason": "Not archived on Wayback and Cloudflare blocks live fetch. "
                          "Open the page in your browser and use the bookmarklet or paste mode.",
            })
        tracks = parse_tracks(html)
        return jsonify({"type": "tracklist", "url": url, "source": source,
                        "title": parse_set_title(html), "tracks": tracks})

    if YT_URL_RE.search(q):
        try:
            r = resolve_youtube(q)
        except Exception as e:
            return jsonify({"error": f"YouTube fetch failed: {e}"}), 502
        if len(r["tracks"]) < 3:
            return jsonify({
                "type": "manual_needed", "url": q, "can_shazam": True,
                "tl_url": r.get("tl_url"),
                "reason": "No readable tracklist in this video's chapters, description, "
                          "linked Apple Music album, 1001tracklists, or top comments. "
                          "Shazam-tag it below, or paste a tracklist if you find one.",
            })
        return jsonify(r)

    am = APPLE_ALBUM_RE.search(q)
    if am:
        try:
            title, tracks = apple_album_tracks(am.group(0))
        except Exception as e:
            return jsonify({"error": f"Apple Music fetch failed: {e}"}), 502
        if not tracks:
            return jsonify({"error": "No tracklist found on that Apple Music page"}), 422
        return jsonify({"type": "tracklist", "url": q, "source": "apple music album",
                        "title": title, "tracks": tracks})

    dj = DJ_URL_RE.search(q)
    if dj or ("/" not in q and len(q) > 1):
        if dj:
            dj_slug = dj.group(1)
            name = dj_display_name(dj_slug)
        else:
            name = q
        sets = ddg_sets_for(name)
        # DJ page snapshot may list sets CDX misses
        if dj:
            html, _ = fetch_page(f"https://www.1001tracklists.com/dj/{dj.group(1)}/index.html")
            if html:
                known = {s["id"] for s in sets}
                for link in parse_tracklist_links(html):
                    m = TL_URL_RE.search(link["url"])
                    if m and m.group(1) not in known:
                        title, date = slug_to_title(link["slug"])
                        sets.append({"url": link["url"], "id": m.group(1),
                                     "title": title, "date": date, "archived": ""})
        sets.sort(key=lambda s: s["date"], reverse=True)
        return jsonify({"type": "dj", "name": name, "sets": sets})

    if q.startswith("http"):
        # Instagram reel, TikTok, SoundCloud… — no tracklist to read, but the
        # audio itself is taggable
        return jsonify({
            "type": "manual_needed", "url": q, "can_shazam": True,
            "reason": "No tracklist source for this link — Shazam the audio instead "
                      "(works for Instagram reels, TikToks, SoundCloud, anything yt-dlp can reach).",
        })

    return jsonify({"error": "Drop a 1001tracklists tracklist/DJ URL, a YouTube set, "
                    "an Apple Music album, or a DJ name"}), 400


@app.route("/api/ingest", methods=["POST", "OPTIONS"])
def ingest():
    """Bookmarklet / paste fallback: raw HTML or plain 'Artist - Title' lines."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.json or {}
    raw = data.get("html", "") or data.get("text", "")
    if not raw:
        return jsonify({"error": "html or text required"}), 400
    tracks = parse_tracks(raw)
    title = parse_set_title(raw) if "<" in raw else ""
    if not tracks:  # plain text: one track per line
        tracks = tracks_from_lines(raw.splitlines())
    if not tracks:
        return jsonify({"error": "No tracks found in that content"}), 422
    result = {"type": "tracklist", "url": data.get("url", ""), "source": "manual",
              "title": title or data.get("title") or "Pasted setlist",
              "tracks": tracks}
    ingest_id = hashlib.md5(f"{time.time()}".encode()).hexdigest()[:8]
    cache_put("ingest:" + ingest_id, result)
    return jsonify({"id": ingest_id, **result})


@app.route("/api/ingest/<ingest_id>")
def ingest_get(ingest_id):
    result = cache_get("ingest:" + ingest_id)
    if not result:
        return jsonify({"error": "Not found"}), 404
    return jsonify(result)


@app.route("/api/match", methods=["POST"])
def match():
    """Spotify-match a list of tracks. Called per-batch by the UI."""
    tracks = (request.json or {}).get("tracks", [])
    out = []
    for t in tracks:
        if t.get("unknown"):
            out.append({**t, "match": None, "match_status": "id_track"})
            continue
        m = spotify_match(t["artist"], t["title"])
        if m:
            out.append({**t, "match": m, "match_status": "spotify"})
            continue
        it = itunes_match(t["artist"], t["title"])
        if it:
            out.append({**t, "match": None, "verified": it, "match_status": "verified"})
        else:
            out.append({**t, "match": None, "match_status": "youtube_fallback"})
    return jsonify({"tracks": out})


@app.route("/api/download", methods=["POST"])
def download():
    data = request.json or {}
    name = (data.get("name") or "Setlist").strip()
    items = data.get("tracks", [])
    if not items:
        return jsonify({"error": "tracks required"}), 400
    if not amapiano_up():
        return jsonify({"error": "amapiano server is not running — start it: "
                        "cd ~/amapiano && source .venv/bin/activate && python server.py &"}), 503
    job_id = hashlib.md5(f"{name}{time.time()}".encode()).hexdigest()[:10]
    job = {"id": job_id, "name": name, "status": "running", "started": time.time(),
           "items": [{
               "n": t.get("n"), "artist": t.get("artist", ""), "title": t.get("title", ""),
               "name": t.get("name", ""), "spotify_url": (t.get("match") or {}).get("url"),
               "duration_s": ((t.get("match") or t.get("verified") or {}).get("duration_s")
                              or t.get("duration_s")),
               "t": t.get("t"),
               "skip": bool(t.get("unknown") or t.get("skip")), "status": "queued",
           } for t in items]}
    with _jobs_lock:
        _jobs[job_id] = job
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return jsonify({"id": job_id, "count": len(job["items"])})


@app.route("/api/job/<job_id>")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    print(f"Setlist running on http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
