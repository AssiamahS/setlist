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
- Spotify matching uses the spotdl app creds from `~/.spotdl/config.json`
  (client_credentials flow; the pair hardcoded in amapiano/server.py died — this one works).
- Downloads: POST per track to amapiano `/api/download` with name = set title, serial
  (waits for each before submitting the next). Spotify URL first; if no match,
  resolves a real YouTube watch URL via `yt-dlp --print webpage_url` (never bare
  ytsearch strings). ID - ID tracks are skipped.
- `cache/` holds Wayback pages (6h), CDX set lists (24h), Spotify/YouTube matches (forever).
