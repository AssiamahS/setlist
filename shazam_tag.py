#!/usr/bin/env python3
"""Tag a DJ mix by Shazam-sampling it: fingerprint a short clip every STRIDE
seconds and print one JSON line per sample so the caller can stream progress.

Run this with a Python that has a working shazamio — setlist's own venv is
Python 3.14 where shazamio-core segfaults; cratemate's 3.13 venv works:
  ~/cratemate/.venv/bin/python shazam_tag.py AUDIO [STRIDE] [CLIP]

Output (JSONL on stdout):
  {"i": 3, "total": 80, "t": 90, "artist": "...", "title": "..."}
  {"done": true}
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile


def duration_s(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30).stdout.strip()
    return float(out or 0)


async def main():
    audio = sys.argv[1]
    stride = int(sys.argv[2]) if len(sys.argv) > 2 else 45
    clip = int(sys.argv[3]) if len(sys.argv) > 3 else 10
    dur = int(duration_s(audio))
    if dur < stride * 4:  # short clip (reel/TikTok): sample densely
        stride = max(12, dur // 5 or 12)
    offsets = list(range(0, max(dur - clip, 1), stride))

    from shazamio import Shazam
    sh = Shazam()
    for i, off in enumerate(offsets, 1):
        rec = {"i": i, "total": len(offsets), "t": off, "artist": "", "title": ""}
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-ss", str(off), "-t", str(clip),
                 "-i", audio, "-ar", "16000", "-ac", "1", wav],
                capture_output=True, timeout=60)
            r = await asyncio.wait_for(sh.recognize(wav), timeout=25)
            t = r.get("track") or {}
            rec["artist"] = t.get("subtitle", "")
            rec["title"] = t.get("title", "")
        except Exception as e:
            rec["error"] = str(e)[:100]
        finally:
            try:
                os.remove(wav)
            except OSError:
                pass
        print(json.dumps(rec), flush=True)
        await asyncio.sleep(0.3)  # be polite to Shazam or it starts refusing
    print(json.dumps({"done": True}), flush=True)


asyncio.run(main())
