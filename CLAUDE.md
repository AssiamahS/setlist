# Setlist

Drop a 1001tracklists URL (tracklist or DJ page) → pulls every track → downloads
through the amapiano server into the shared library / Serato crates / rekordbox.

## Quick start
- Server: `cd /Users/djsly/setlist && source .venv/bin/activate && python server.py &`
- URL: http://localhost:8787
- Needs the amapiano server running on :8766 for downloads (parsing/matching work without it).

## How it works
- 1001tracklists is behind Cloudflare Turnstile — live fetches get a challenge shell.
  Fetch order: direct → Wayback Machine latest snapshot → manual (bookmarklet/paste).
- DJ URL or plain DJ name → Wayback CDX search over `1001tracklists.com/tracklist*`
  for slugs containing the hyphenated DJ name → list of archived sets.
- Tracks parsed from schema.org `MusicRecording` metas (also works on pasted page HTML).
- YouTube set URL → yt-dlp -J: chapters → description lines → Apple Music album link
  in description (page JSON-LD has the full mix tracklist) → top-40 comments.
  Apple Music album URLs also accepted directly. IDn tracks flagged unknown/skipped.
- Timestamps: chapters carry start_time; description/comment/pasted lines keep their
  leading ("23:10 Artist - Title") or trailing cue as `t` seconds; Apple Music albums
  get cumulative segment starts. UI shows a Time column.
- Shazam tagging (`POST /api/shazam {url}`, poll `/api/shazam/<id>`): for mixes with
  no posted tracklist (or to re-tag with timestamps). Downloads the mix audio to
  cache/audio/<videoid>.*, then `shazam_tag.py` fingerprints a 10s clip every 45s and
  streams JSONL; consecutive identical tags merge into one track at the first hit's
  offset. shazamio segfaults on this venv's Python 3.14 — the helper runs under
  ~/cratemate/.venv/bin/python (3.13). Results cached per video id (`force` to redo).
- Per-track review in the UI: ✓/✗ toggle (✗ = not its own song — the span folds into
  the next kept track's transition, whose start time inherits it; sent as `skip`) and
  ✎ edit (re-matches that row via /api/match).
- Spotify matching uses the spotdl app creds from `~/.spotdl/config.json`
  (client_credentials flow; the pair hardcoded in amapiano/server.py died — this one works).
- Downloads: POST per track to amapiano `/api/download` with name = set title, serial
  (waits for each before submitting the next). Spotify URL first; if no match,
  resolves a real YouTube watch URL via `yt-dlp --print webpage_url` (never bare
  ytsearch strings). ID - ID tracks are skipped.
- After a download job, setlist asks amapiano to export the set's playlist as a Serato
  crate (`/api/serato/export`) and shows the result. amapiano also writes the crate
  itself per download and auto-syncs rekordbox XML, so this is confirmation + final
  full-list rewrite.
- `cache/` holds Wayback pages (6h), CDX set lists (24h), Spotify/YouTube matches and
  Shazam tracklists (forever), downloaded mix audio in `cache/audio/`.
