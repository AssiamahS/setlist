# setlist

Drop a [1001tracklists](https://www.1001tracklists.com) URL, get every track the DJ
played, and download them into your library.

- **Tracklist URL** → the full set, track by track
- **DJ URL or just a name** (e.g. `james hype`) → every archived set they've played,
  click into any of them
- **YouTube set URL** → tracklist from the video's chapters, description, the Apple
  Music mix album it links, or the top comments
- **Apple Music album URL** → the album/DJ-mix tracklist directly
- **Paste mode / bookmarklet** → for brand-new sets that aren't archived yet

Each track is matched on Spotify first (clean metadata); unmatched tracks fall back
to a resolved YouTube URL. Downloads run through the amapiano library server, so
everything lands in the same folders, Serato crates and rekordbox XML.

## Run

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
# → http://localhost:8787
```

Requires `yt-dlp` on PATH and the amapiano server on `localhost:8766` for downloads.

## Why Wayback?

1001tracklists sits behind Cloudflare Turnstile, which blocks every kind of
automated fetch (curl, TLS-impersonation, headless and headed browsers). The site
is heavily archived though, so pages are pulled from the Wayback Machine — and for
sets too new to be archived, the bookmarklet grabs the page straight out of your
own browser tab.
