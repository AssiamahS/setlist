"""Passive MIDI wiretap for djdreck set recording.

Opens every MIDI input port matching --port (CoreMIDI allows multiple
clients, so this listens alongside Serato without stealing the device)
and appends one JSON line per event to --out:

    {"meta": {"t0": <unix>, "ports": ["DDJ-SB3"]}}   <- first line
    [t_rel_seconds, status, data1, data2]            <- every event

Runs under a Python 3.13 interpreter (python-rtmidi lives in the
cratemate venv — the setlist 3.14 venv can't build it). Killed with
SIGTERM by the setlist server; flushes on every write so the live
status endpoint can tail the file.
"""
import argparse
import json
import signal
import sys
import time

import rtmidi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="DDJ", help="substring of MIDI input port name(s)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    probe = rtmidi.MidiIn()
    names = probe.get_ports()
    del probe
    wanted = [(i, n) for i, n in enumerate(names) if args.port.lower() in n.lower()]
    if not wanted:
        print(f"no MIDI input matching {args.port!r} in {names}", file=sys.stderr)
        sys.exit(2)

    out = open(args.out, "a", buffering=1)
    t0 = time.time()
    out.write(json.dumps({"meta": {"t0": t0, "ports": [n for _, n in wanted]}}) + "\n")

    start = time.monotonic()
    inputs = []

    def make_cb(port_name):
        def cb(event, _data=None):
            msg, _delta = event
            if not msg or msg[0] >= 0xF0:  # skip realtime/sysex
                return
            t = round(time.monotonic() - start, 4)
            out.write(json.dumps([t, msg[0], msg[1] if len(msg) > 1 else 0,
                                  msg[2] if len(msg) > 2 else 0]) + "\n")
        return cb

    for idx, name in wanted:
        mi = rtmidi.MidiIn()
        mi.open_port(idx)
        mi.set_callback(make_cb(name))
        inputs.append(mi)

    running = True

    def stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        time.sleep(0.2)

    for mi in inputs:
        mi.close_port()
    out.close()


if __name__ == "__main__":
    main()
