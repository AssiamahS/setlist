"""djdreck — record a live DJ set from the DDJ-SB3 while Serato plays.

Two data streams, zero interference with Serato:
  1. MIDI wiretap (midi_capture.py under the cratemate 3.13 venv) — every
     stroke on the controller: crossfader, EQ, jogs, pads, play.
  2. Serato's own history session files (~/Music/_Serato_/History/Sessions/
     *.session) — which track was on which deck, with unix start/end times
     and the real file path. Because these are the actual files played,
     the resulting crate is exact — no Shazam, no matching.

On stop the played tracks become a Serato crate under the "DJ Dwrek"
parent crate. Serato flushes history entries when a track is ejected or
on quit, so a "rescan" rebuild exists for tracks that land late.
"""
import json
import os
import re
import signal
import struct
import subprocess
import time
import uuid
from pathlib import Path

BASE = Path(__file__).parent
REC_DIR = BASE / "recordings"
REC_DIR.mkdir(exist_ok=True)

SESSIONS_DIR = Path.home() / "Music" / "_Serato_" / "History" / "Sessions"
SUBCRATES_DIR = Path.home() / "Music" / "_Serato_" / "Subcrates"
CRATE_FOLDER = "DJ Dwrek"

CAPTURE_PY = Path.home() / "cratemate" / ".venv" / "bin" / "python"  # 3.13 w/ python-rtmidi
CAPTURE_SCRIPT = BASE / "midi_capture.py"

_active = {"id": None, "proc": None}


# ── Serato .session history parser ──
# oent chunks wrap adat chunks; adat is a list of (u32 field id, u32 len, value).
_ADAT_FIELDS = {
    2: ("path", "str"), 6: ("title", "str"), 7: ("artist", "str"),
    8: ("album", "str"), 9: ("genre", "str"), 15: ("duration", "str"),
    28: ("start_ts", "u32"), 29: ("end_ts", "u32"),
    31: ("deck", "u32"), 50: ("played", "u8"),
}


def _parse_adat(data):
    out, i = {}, 0
    while i + 8 <= len(data):
        fid, flen = struct.unpack(">II", data[i : i + 8])
        if flen > len(data) - i - 8:
            break
        val = data[i + 8 : i + 8 + flen]
        name, typ = _ADAT_FIELDS.get(fid, (None, None))
        if name:
            if typ == "str":
                out[name] = val.decode("utf-16-be", "replace").rstrip("\x00")
            elif typ == "u32" and flen == 4:
                out[name] = struct.unpack(">I", val)[0]
            elif typ == "u8" and flen == 1:
                out[name] = val[0]
        i += 8 + flen
    return out


def parse_session_file(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    entries, i = [], 0
    while i + 8 <= len(data):
        tag = data[i : i + 4]
        (ln,) = struct.unpack(">I", data[i + 4 : i + 8])
        if ln > len(data) - i - 8:
            break
        body = data[i + 8 : i + 8 + ln]
        if tag == b"oent" and body[:4] == b"adat":
            (alen,) = struct.unpack(">I", body[4:8])
            e = _parse_adat(body[8 : 8 + alen])
            if e.get("path"):
                entries.append(e)
        i += 8 + ln
    return entries


def tracks_between(t_start, t_end):
    """Deduped tracks whose play overlaps [t_start, t_end], from every
    session file touched since the recording began."""
    seen = {}
    for f in SESSIONS_DIR.glob("*.session"):
        if f.stat().st_mtime < t_start - 60:
            continue
        for e in parse_session_file(f):
            st = e.get("start_ts") or 0
            en = e.get("end_ts") or t_end  # still on deck = no end yet
            if st <= t_end and en >= t_start:
                seen[(e["path"], st)] = e  # later rewrites win (carry end_ts)
    out = sorted(seen.values(), key=lambda e: e.get("start_ts") or 0)
    for e in out:
        e["exists"] = os.path.exists(e["path"])
    return out


# ── Serato crate writer (same binary format amapiano uses) ──
def _crate_bytes(track_paths):
    buf = bytearray()
    ver = "1.0/Serato ScratchLive Crate".encode("utf-16-be")
    buf += b"vrsn" + struct.pack(">I", len(ver)) + ver
    for path in track_paths:
        p = path.lstrip("/")
        pb = p.encode("utf-16-be")
        buf += b"otrk" + struct.pack(">I", len(pb) + 8)
        buf += b"ptrk" + struct.pack(">I", len(pb)) + pb
    return bytes(buf)


def write_crate(set_name, track_paths):
    SUBCRATES_DIR.mkdir(parents=True, exist_ok=True)
    parent = SUBCRATES_DIR / f"{CRATE_FOLDER}.crate"
    if not parent.exists():
        parent.write_bytes(_crate_bytes([]))
    safe = re.sub(r"[/%:]", "-", set_name).strip() or "untitled"
    dest = SUBCRATES_DIR / f"{CRATE_FOLDER}%%{safe}.crate"
    dest.write_bytes(_crate_bytes(track_paths))
    return str(dest)


# ── recording lifecycle ──
def _meta_path(rid):
    return REC_DIR / rid / "meta.json"


def _load_meta(rid):
    try:
        return json.loads(_meta_path(rid).read_text())
    except OSError:
        return None


def _save_meta(meta):
    _meta_path(meta["id"]).write_text(json.dumps(meta, indent=1))


def start(name, port="DDJ"):
    if _active["proc"] and _active["proc"].poll() is None:
        return {"error": "already recording", "id": _active["id"]}
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    d = REC_DIR / rid
    d.mkdir()
    proc = subprocess.Popen(
        [str(CAPTURE_PY), str(CAPTURE_SCRIPT), "--port", port, "--out", str(d / "midi.jsonl")],
        stdout=subprocess.DEVNULL, stderr=open(d / "capture.err", "w"))
    time.sleep(0.6)
    if proc.poll() is not None:  # died immediately — no matching port
        err = (d / "capture.err").read_text()[:300]
        return {"error": f"capture failed: {err or 'controller not found'}"}
    meta = {"id": rid, "name": name or rid, "started": time.time(),
            "status": "recording", "port": port}
    _save_meta(meta)
    _active.update(id=rid, proc=proc)
    return meta


def _event_count(rid):
    try:
        with open(REC_DIR / rid / "midi.jsonl", "rb") as f:
            return sum(1 for _ in f) - 1  # minus meta line
    except OSError:
        return 0


def _tail_labeled(rid, n=10):
    """Label the last few raw events for the live feed while recording."""
    try:
        with open(REC_DIR / rid / "midi.jsonl", "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4096))
            lines = f.read().decode("utf-8", "replace").splitlines()[-n * 4:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            continue
        t, st, d1, d2 = ev
        kind, ch = st & 0xF0, st & 0x0F
        if kind == 0x90 and d2 > 0:
            label, group, deck = _label_note(ch, d1)
        elif kind == 0xB0 and (ch, d1) not in _LSB_CCS:
            label, group, deck = _label_cc(ch, d1)
        else:
            continue
        if out and out[-1]["label"] == label and out[-1]["deck"] == deck:
            out[-1]["t"] = t
            continue
        out.append({"t": t, "label": label, "group": group, "deck": deck})
    return out[-n:]


def status():
    rid = _active["id"]
    if not rid or not _active["proc"] or _active["proc"].poll() is not None:
        return {"recording": False}
    meta = _load_meta(rid)
    now = time.time()
    return {
        "recording": True, "id": rid, "name": meta["name"],
        "elapsed": round(now - meta["started"], 1),
        "events": _event_count(rid),
        "feed": _tail_labeled(rid),
        "tracks": tracks_between(meta["started"], now),
    }


def stop():
    rid = _active["id"]
    proc = _active["proc"]
    if not rid or not proc:
        return {"error": "not recording"}
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _active.update(id=None, proc=None)
    meta = _load_meta(rid)
    meta["ended"] = time.time()
    meta["status"] = "done"
    meta["events"] = _event_count(rid)
    tracks = tracks_between(meta["started"], meta["ended"])
    (REC_DIR / rid / "tracks.json").write_text(json.dumps(tracks, indent=1))
    playable = [t["path"] for t in tracks if t["exists"]]
    if playable:
        meta["crate"] = write_crate(meta["name"], playable)
    meta["track_count"] = len(tracks)
    _save_meta(meta)
    return {**meta, "tracks": tracks}


def rescan(rid):
    """Re-pull tracks from Serato history (it flushes entries on track eject
    and on quit, so late entries appear after the fact) and rewrite the crate."""
    meta = _load_meta(rid)
    if not meta:
        return {"error": "recording not found"}
    ended = meta.get("ended") or time.time()
    tracks = tracks_between(meta["started"], ended + 300)
    (REC_DIR / rid / "tracks.json").write_text(json.dumps(tracks, indent=1))
    playable = [t["path"] for t in tracks if t["exists"]]
    if playable:
        meta["crate"] = write_crate(meta["name"], playable)
    meta["track_count"] = len(tracks)
    _save_meta(meta)
    return {**meta, "tracks": tracks}


def list_recordings():
    out = []
    for d in sorted(REC_DIR.iterdir(), reverse=True):
        m = _load_meta(d.name)
        if m:
            out.append(m)
    return out


def get_recording(rid):
    meta = _load_meta(rid)
    if not meta:
        return None
    try:
        tracks = json.loads((REC_DIR / rid / "tracks.json").read_text())
    except OSError:
        tracks = []
    return {**meta, "tracks": tracks}


# ── stroke gestures for the replay view ──
# DDJ-SB3 map confirmed by live wiretap 2026-07-15 (channels 1-based):
#   crossfader ch7 cc31 (cc63 = LSB), EQ ch5/ch6 cc2/34 cc4/36 cc6/38,
#   jog spin ch1/ch2 cc34-35 + jog touch note 54, play note 11,
#   pads ch8/ch9, FX ch11/ch12.
_LSB_CCS = {(6, 63), (4, 34), (4, 36), (4, 38), (5, 34), (5, 36), (5, 38)}  # 0-based ch
_EQ_NAME = {2: "HI", 4: "MID", 6: "LOW"}


def _label_cc(ch, cc):
    """(label, group, deck) for a 0-based channel + cc."""
    if ch == 6 and cc == 31:
        return "XFADE", "xfade", 0
    if ch in (4, 5) and cc in _EQ_NAME:
        return _EQ_NAME[cc], "eq", 1 if ch == 4 else 2
    if ch in (0, 1) and cc in (33, 34, 35):
        return "JOG", "jog", ch + 1
    if ch in (10, 11):
        return f"FX{cc}", "fx", ch - 9
    return f"CC{cc}", "cc", ch + 1


def _label_note(ch, note):
    if note == 11:
        return "PLAY", "play", ch + 1 if ch in (0, 1) else 0
    if note == 54 and ch in (0, 1):
        return "TOUCH", "jog", ch + 1
    if ch in (7, 8):
        pad = note % 8
        return ("ABXY"[pad] if pad < 4 else f"P{pad + 1}"), "pad", ch - 6
    return f"N{note}", "note", ch + 1


def gestures(rid, gap=0.25):
    """Compact the raw wiretap into replay gestures: notes become presses,
    runs of the same CC within `gap` seconds coalesce into one move."""
    path = REC_DIR / rid / "midi.jsonl"
    if not path.exists():
        return []
    presses, runs, open_runs = [], [], {}
    with open(path) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                continue  # meta line
            t, st, d1, d2 = ev
            kind, ch = st & 0xF0, st & 0x0F
            if kind == 0x90 and d2 > 0:
                label, group, deck = _label_note(ch, d1)
                presses.append({"t": t, "label": label, "group": group, "deck": deck})
            elif kind == 0xB0:
                if (ch, d1) in _LSB_CCS:
                    continue
                key = (ch, d1)
                label, group, deck = _label_cc(ch, d1)
                r = open_runs.get(key)
                jog_dir = (1 if d2 > 64 else -1) if group == "jog" else 0
                if r and t - r["t1"] <= gap and (group != "jog" or jog_dir == r.get("dir")):
                    r["t1"], r["to"], r["n"] = t, d2, r["n"] + 1
                else:
                    if r:
                        runs.append(r)
                    r = {"t": t, "t1": t, "label": label, "group": group,
                         "deck": deck, "from": d2, "to": d2, "n": 1}
                    if group == "jog":
                        r["dir"] = jog_dir
                    open_runs[key] = r
    runs.extend(open_runs.values())
    out = presses + runs
    out.sort(key=lambda g: g["t"])
    return out
