# Twitch Clipping Tool

Records a Twitch stream, watches chat for spikes, and cuts a 30-second clip around each one, with captions.

Pipeline: streamlink (records) -> Twitch chat (read anonymously) -> clipper.py (counts messages every 10s, finds spikes) -> ffmpeg (cuts 30s around each spike) -> whisper (captions)

## Setup (Windows)

1. Install Python 3.9+ from python.org. Tick "Add Python to PATH" in the installer.
2. Install ffmpeg: `winget install ffmpeg`, then close and reopen the terminal. Check with `ffmpeg -version`.
3. Install the rest: `pip install streamlink openai-whisper` (or `python -m pip install streamlink openai-whisper`).
4. Put `clipper.py` in a folder and open a terminal there.

## Run

```
python clipper.py CHANNEL_NAME
```

- Start it before the streamer goes live. It waits, then records.
- Leave the terminal open. Do not close the window.
- Press Ctrl+C once to stop, or let the stream end.
- Wait for `[done]`. Clips are in `sessions/CHANNEL-<date>/clips/`.

Clips are cut after the stream ends, not during the live.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| --length 45 | 30 | Clip length in seconds |
| --z 1.5 | 2.0 | Spike sensitivity (lower = more clips) |
| --min-count 15 | 15 | Min messages per 10s to count as a spike |
| --lag 5 | 5 | Seconds clips are shifted earlier |
| --model small | base | Whisper model (bigger = slower, more accurate) |
| --burn | off | Also save a copy with captions burned in |

Re-cut a saved session: `python clipper.py --reprocess sessions\CHANNEL-<date> --z 1.5`

## Cleanup

- Delete `recording.ts` after `[done]` and after checking the clips (a few GB per hour). You can't `--reprocess` without it.
- Delete clips once they are posted and playing.

## Troubleshooting

- streamlink not found: `pip install streamlink`
- ffmpeg not found: `winget install ffmpeg`, reopen terminal
- 0 clips: lower `--z` (e.g. 1.5) or `--min-count` (e.g. 8)
- Too many junk clips: raise `--z` to 3
- Clips start late: raise `--lag`
- No captions: `pip install openai-whisper`
