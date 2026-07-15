# Setlist

Drop a 1001tracklists URL (tracklist or DJ page) → pulls every track → downloads
through the amapiano server into the shared library / Serato crates / rekordbox.

## DWREK set recorder (`/recorder`, "⏺ DWREK" link in the header)
- Records a live set on the DDJ-SB3 while Serato DJ Pro owns it: `midi_capture.py`
  (runs under `~/cratemate/.venv/bin/python` — python-rtmidi won't build on this
  venv's 3.14) opens the controller's input as a second CoreMIDI client, so it's a
  passive tap; Serato is unaffected. Raw events land in `recordings/<id>/midi.jsonl`.
- Tracks come from Serato's own history session files
  (`~/Music/_Serato_/History/Sessions/*.session`, oent/adat binary chunks —
  parser in `recorder.py`): real file path, artist/title, deck, unix start/end.
  Serato flushes entries on track eject and on quit, so the last track can land
  late — the ⟳ rescan button re-parses and rewrites the crate.
- On stop, played tracks are written as a Serato crate
  `~/Music/_Serato_/Subcrates/DJ Dwrek%%<set name>.crate` (paths that still exist
  only). Serato shows it after restart. No matching/Shazam needed — these are the
  exact files played.
- Replay: `/api/recorder/recordings/<id>/events` compacts the raw stream into
  gestures (notes = presses, CC runs within 250ms coalesce); the arena page plays
  them back Mortal-Kombat-style — jogs spin, pads A/B/X/Y flash, crossfader slides,
  combo chip feed scrolls. Deep link: `/recorder?replay=<id>&t=<s>&play=1`.
- DDJ-SB3 map (confirmed by live wiretap 2026-07-15, 1-based channels): crossfader
  ch7 cc31 (+cc63 LSB), EQ ch5/ch6 cc2/4/6 (+34/36/38 LSBs), jog ch1/ch2 cc34-35 +
  touch note54, play note11, pads ch8/ch9, FX ch11/ch12.
- `recordings/` is gitignored (personal set data).

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
