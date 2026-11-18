#!/usr/bin/env python3
"""
clipper.py - auto-clip Twitch streams from chat spikes.

Pipeline:
  streamlink -> records the stream the moment it goes live
  twitch chat -> read anonymously over IRC (no account, no token)
  this script -> counts chat messages every 10 seconds, finds spikes
  ffmpeg      -> cuts 30 seconds around every spike
  whisper     -> writes captions (.srt) for each clip

Usage:
  python clipper.py CHANNEL                 # wait for live, record, clip when stream ends / Ctrl+C
  python clipper.py CHANNEL --burn          # also burn captions into the video
  python clipper.py --reprocess SESSION_DIR # re-run spike detection + clipping on a saved session

Requires on PATH: streamlink, ffmpeg, whisper (openai-whisper CLI).
"""
import argparse
import json
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

BIN = 10  # seconds per chat-count bucket


def _has_module(name):
    import importlib.util
    return importlib.util.find_spec(name) is not None


# ---------- chat (anonymous read-only IRC) ----------
def chat_reader(channel, stamps, stop):
    nick = "justinfan%d" % (int(time.time()) % 80000 + 1000)
    while not stop.is_set():
        try:
            raw = socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=30)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname="irc.chat.twitch.tv")
            sock.settimeout(30)
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel.lower()}\r\n".encode())
            buf = b""
            while not stop.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    if line.startswith(b"PING"):
                        sock.sendall(b"PONG :tmi.twitch.tv\r\n")
                    elif b" PRIVMSG " in line:
                        stamps.append(time.time())
        except OSError as e:
            print(f"[chat] connection problem ({e}), retrying in 5s", file=sys.stderr)
            time.sleep(5)


# ---------- recording ----------
def record(channel, out_path, stop):
    sl = ["streamlink"] if shutil.which("streamlink") else [sys.executable, "-m", "streamlink"]
    cmd = sl + ["--retry-streams", "10", "--retry-max", "0",
           "-o", str(out_path), f"twitch.tv/{channel}", "best"]
    print("[rec] waiting for stream to go live...")
    proc = subprocess.Popen(cmd)
    rec_start = None
    try:
        while proc.poll() is None:
            if rec_start is None and out_path.exists() and out_path.stat().st_size > 0:
                rec_start = time.time()
                print("[rec] live - recording started")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[rec] stopping...")
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    stop.set()
    return rec_start


# ---------- spike detection ----------
def find_spikes(offsets, z=2.0, min_count=15, merge_gap=30):
    """offsets: message times in seconds since recording start. Returns peak times (s)."""
    if not offsets:
        return []
    n = int(max(offsets) // BIN) + 1
    counts = [0] * n
    for t in offsets:
        if t >= 0:
            counts[int(t // BIN)] += 1
    mean = statistics.fmean(counts)
    sd = statistics.pstdev(counts) or 1.0
    thresh = max(mean + z * sd, min_count)
    hot = [i for i, c in enumerate(counts) if c >= thresh]
    # merge neighbouring hot bins into one spike, keep the peak bucket
    spikes, group = [], []
    for i in hot:
        if group and (i - group[-1]) * BIN > merge_gap:
            spikes.append(group)
            group = []
        group.append(i)
    if group:
        spikes.append(group)
    peaks = [max(g, key=lambda i: counts[i]) for g in spikes]
    print(f"[spikes] {len(peaks)} found (threshold {thresh:.0f} msgs/{BIN}s, mean {mean:.1f})")
    return [p * BIN + BIN / 2 for p in peaks]


# ---------- clipping + captions ----------
def write_description(clips_dir, name, channel):
    """Write <name>_tiktok.txt: a caption built from the whisper transcript + hashtags."""
    import re
    srt = clips_dir / f"{name}.srt"
    text = ""
    if srt.exists():
        lines = [l.strip() for l in srt.read_text(encoding="utf-8", errors="ignore").splitlines()]
        spoken = [l for l in lines if l and not l.isdigit() and "-->" not in l]
        text = " ".join(spoken)
    hook = ""
    if text:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        hook = next((s for s in sentences if len(s) >= 25), text)[:90].strip(' ".,!?')
    tags = ["#twitch", f"#{channel.lower()}", "#twitchclips", "#streamerclips",
            "#gaming", "#fyp", "#viral"]
    desc = (f"{hook}... \U0001F440\n" if hook else "Chat went CRAZY \U0001F525\n")
    desc += f"\U0001F4FA {channel} on Twitch - clipped from live chat spike\n\n" + " ".join(tags)
    (clips_dir / f"{name}_tiktok.txt").write_text(desc, encoding="utf-8")
    print(f"[tiktok] {name}_tiktok.txt")


def make_clip(rec, peak, idx, clips_dir, length, lag, whisper_model, burn, channel="stream"):
    start = max(0.0, peak - lag - length / 2)
    name = f"clip_{idx:03d}_{int(start // 60):02d}m{int(start % 60):02d}s"
    mp4 = clips_dir / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(rec),
                    "-t", str(length), "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", str(mp4)], check=True)
    print(f"[clip] {mp4.name}")
    wh = ["whisper"] if shutil.which("whisper") else [sys.executable, "-m", "whisper"]
    if shutil.which("whisper") or _has_module("whisper"):
        subprocess.run(wh + [mp4.name, "--model", whisper_model,
                        "--output_format", "srt", "--output_dir", "."],
                       cwd=clips_dir, check=False)
        srt = clips_dir / f"{name}.srt"
        if burn and srt.exists():
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4.name,
                            "-vf", f"subtitles={srt.name}", "-c:a", "copy", f"{name}_captioned.mp4"],
                           cwd=clips_dir, check=False)
    else:
        print("[whisper] not found on PATH - skipping captions")
    write_description(clips_dir, name, channel)


def process(session, args):
    rec = session / "recording.ts"
    data = json.loads((session / "chat.json").read_text())
    peaks = find_spikes(data["offsets"], args.z, args.min_count)
    clips_dir = session / "clips"
    clips_dir.mkdir(exist_ok=True)
    for i, p in enumerate(peaks, 1):
        make_clip(rec, p, i, clips_dir, args.length, args.lag, args.model, args.burn,
                  data.get("channel") or session.name.rsplit("-", 2)[0])
    print(f"[done] {len(peaks)} clips in {clips_dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("channel", nargs="?", help="Twitch channel name")
    ap.add_argument("--reprocess", metavar="DIR", help="re-run clipping on a saved session folder")
    ap.add_argument("--length", type=int, default=30, help="clip length in seconds (default 30)")
    ap.add_argument("--z", type=float, default=2.0, help="spike sensitivity, lower = more clips (default 2.0)")
    ap.add_argument("--min-count", type=int, default=15, help="min messages per 10s to count as a spike")
    ap.add_argument("--lag", type=float, default=5.0, help="seconds chat reacts after the moment (default 5)")
    ap.add_argument("--model", default="base", help="whisper model (tiny/base/small/medium)")
    ap.add_argument("--burn", action="store_true", help="burn captions into a second video file")
    ap.add_argument("--out", default="sessions", help="output folder")
    args = ap.parse_args()

    if args.channel:  # accept a full URL too
        args.channel = args.channel.rstrip("/").split("/")[-1].split("?")[0]

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found on PATH")
    if not args.reprocess and not shutil.which("streamlink"):
        try:
            import streamlink  # noqa: F401  (works via python -m streamlink)
        except ImportError:
            sys.exit("streamlink not installed. Run: pip install streamlink")

    if args.reprocess:
        return process(Path(args.reprocess), args)
    if not args.channel:
        ap.error("give a channel name or --reprocess DIR")

    session = Path(args.out) / f"{args.channel}-{time.strftime('%Y%m%d-%H%M%S')}"
    session.mkdir(parents=True)
    stamps, stop = [], threading.Event()
    threading.Thread(target=chat_reader, args=(args.channel, stamps, stop), daemon=True).start()

    rec_start = record(args.channel, session / "recording.ts", stop)
    if rec_start is None:
        sys.exit("stream never started recording")
    offsets = [round(t - rec_start, 2) for t in list(stamps)]
    (session / "chat.json").write_text(json.dumps({"channel": args.channel, "rec_start": rec_start, "offsets": offsets}))
    print(f"[chat] {len(offsets)} messages captured")
    process(session, args)


if __name__ == "__main__":
    main()
