<p align="center">
  <img src="static/assets/logo-transparent.png" alt="FlacPress logo" width="180">
</p>

# FlacPress

Batch-convert lossless music (FLAC / WAV / AIFF / APE / ALAC / WavPack) into
great-sounding lossy formats — Opus, MP3, or AAC — with a small local web UI
on top of a fast, parallel conversion engine.

![license](https://img.shields.io/badge/license-MIT-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue)

<!-- Swap this for a real screenshot or screen-recording GIF of the UI before publishing -->
> 🖼️ *Screenshot / demo GIF goes here*

## Why

Most lossless-to-lossy converters are either a one-off shell script or a
heavyweight GUI app. FlacPress is neither: it's a few hundred lines of Python
you can actually read, wrapped in a small web UI that shows you what's
happening while it happens — each parallel worker gets its own live lane, so
you can watch your library get converted in real time instead of staring at
a spinner.

## Quick start

```bash
git clone https://github.com/<you>/flacpress.git
cd flacpress
pip install -r requirements.txt
python app.py
```

Requires [ffmpeg](https://ffmpeg.org/download.html) on your PATH
(`ffmpeg -version` should work in a terminal). Then open
**http://127.0.0.1:5000**, point it at a music folder — or click **Browse**
to navigate your drives — pick a format, and hit **Start conversion**.

## Features

- **Opus, MP3 (LAME V0), or AAC** output, all with sensible high-quality
  defaults you can override per run
- **Parallel conversion** across as many workers as you want, each shown as
  its own live lane in the UI
- **Smart `.m4a` handling** — `.m4a` can hold lossless ALAC or lossy AAC;
  FlacPress probes the real codec and skips files that are already lossy
  instead of blindly re-encoding them
- **Cover art embedded in every format, including Opus** — Ogg containers
  can't hold a stream-copied image the way MP4/ID3 can, so FlacPress writes
  a proper `METADATA_BLOCK_PICTURE` tag after encoding instead of dropping
  the art
- **Safe by default** — never rescans its own output as new source material,
  even when the output folder lives inside the source folder
- **Real cancellation** — stops in-flight ffmpeg processes immediately, not
  just the queue
- **Dry-run mode** to preview what would happen before it touches a file
- **CLI included** (`cli.py`) if you'd rather not use the browser

## Or just use the terminal

```bash
python cli.py ~/Music --format opus --bitrate 160k --workers 6
python cli.py ~/Music --format mp3 --bitrate 0 --dry-run
```

*(Windows: `python cli.py D:\Music --format opus`)*

## Package as a desktop app

`desktop.py` runs the Flask server in the background and opens it in a
native window via [pywebview](https://pywebview.flowrl.com/) — no browser
tab, no "open localhost:5000" step.

```bash
pip install -r requirements.txt   # pulls in pywebview + pyinstaller too
python desktop.py                  # try it as a normal window first
```

To build a standalone executable:

```bash
pyinstaller --onefile --windowed --name FlacPress --icon static/assets/icon.ico ^
  --add-data "static;static" desktop.py
```

*(on Mac/Linux, use `--add-data "static:static"` — colon, not semicolon)*

The result lands in `dist/FlacPress.exe` (or `dist/FlacPress` on Mac/Linux).
**ffmpeg and ffprobe are not bundled automatically** — PyInstaller only
packages Python code. Drop `ffmpeg.exe` and `ffprobe.exe` in the same
folder as the built executable (FlacPress will pick them up via `shutil.which`
if they're on PATH, or you can point `core.FFMPEG` / `core.FFPROBE` at
bundled copies before building). This is normal for ffmpeg-based tools —
it's what keeps the app itself small instead of a 100MB+ download every
time you rebuild.

## Design notes

**Destination is yours to pick, and it mirrors your folder structure.**
Output no longer lives in a flat `OPUS`/`MP3`/`AAC` folder next to the
source. Choose any destination (defaults to the source folder's parent if
you leave it blank) and FlacPress mirrors the source's directory layout
under it — only the leaf folder that directly contains the audio files
(the album folder) gets the format name appended to its name, e.g.
`Some Album` → `Some Album OPUS`. Everything above that — artist folders,
genre folders, whatever hierarchy you have — is preserved unchanged. See
`resolve_output_path()` in `core.py` for the exact logic and worked
examples.

**All metadata is copied via mutagen, not ffmpeg's `-map_metadata`.**
This turned out to be unreliable in two independent, confirmed ways: some
ffmpeg builds' Ogg muxer write *zero* metadata for Opus/Vorbis output —
not even a hardcoded `-metadata` flag survives — and even where ffmpeg's
mapping does work, multi-valued fields like a second contributing artist
get collapsed into a single semicolon-joined string instead of preserved
as genuinely separate values. `mutagen`'s Easy* interfaces read and write
both correctly, so they're now the single source of truth for every
format's tags (title, artist, album, album artist, track/disc number,
date, genre, composer) — not just Opus's.

**Output never gets rescanned as input.** It's tempting to drop the output
folder inside the source folder — but `.m4a` is both a common lossless
container extension and (for the AAC preset) FlacPress's own output
extension, so a naive recursive scan can find its own previous output and
try to re-encode it as if it were fresh lossless audio. `scan_files()`
explicitly excludes the output directory no matter where it lives, so a
second run — or any AAC run — is always safe to re-run.

**Cover art actually survives Opus conversion now, not just MP3/AAC.**
Ogg containers have no "attached video stream" the way MP4/ID3 containers
do, so the usual `-map 0 -c:v copy` trick can't carry art into a `.opus`
file (this was the original crash — see below). The real Ogg/Vorbis
convention is different: a FLAC-style Picture block, base64-encoded into a
`METADATA_BLOCK_PICTURE` tag. FlacPress extracts the source's embedded
image via ffmpeg, builds that block with `mutagen`, and writes it straight
into the finished `.opus` file as a small post-processing step. Verified
end-to-end against a real FLAC with embedded art: the resulting Opus file
carries the correct MIME type, dimensions, and image bytes, alongside all
the usual title/artist/album tags. Toggle: "Embed cover art".

**Cover-art copy failures no longer take down the whole file.** For MP3/AAC
(which *can* stream-copy art directly), if that copy ever fails for some
other reason — corrupt art, an odd embedded image format — the job
automatically retries once without the video stream instead of failing
outright, and notes in the log that the art was dropped.

**Smart `.m4a` handling.** `.m4a` is an ambiguous container — it can hold
lossless ALAC or lossy AAC. FlacPress probes the actual codec with `ffprobe`
first and skips files that are already lossy AAC (toggle: "Skip
already-lossy .m4a"), so you're not needlessly transcoding lossy-to-lossy
and losing quality a second time.

**Cancellation that actually cancels.** Conversions run via `Popen` with the
process handle tracked per worker slot, so Cancel terminates in-flight
ffmpeg processes immediately instead of waiting for the whole batch to
drain.

**Failures aren't silent.** Every failure captures ffmpeg's stderr and
surfaces it in the log / event stream, so you can see exactly why a file
didn't convert instead of just seeing "failed."

**Output verification** rejects zero-byte files immediately and otherwise
validates every output with `ffprobe` before counting it as done.

**`-nostdin`** is passed to every ffmpeg invocation so it can never hang
waiting on stdin in an environment where that's attached to something
unexpected.

**UI-agnostic core.** `core.py` has no knowledge of Flask, tqdm, or anything
else — it just calls an `on_event` callback with plain dicts. That's what
lets the same engine drive both the web UI (via Server-Sent Events) and
`cli.py` (via tqdm), and makes it straightforward to wire up a desktop GUI
(Tk/Qt) later — just swap the callback.

## Notes on format defaults

| Format | Default | What it means |
|---|---|---|
| Opus | `160k` | Constant target bitrate, VBR mode on — a very strong quality/size ratio |
| MP3 | `0` | LAME `-q:a 0`, i.e. V0 — the highest-quality VBR MP3 setting |
| AAC | `192k` | ffmpeg's native AAC encoder at 192kbps |

All are overridable per run from the UI or `--bitrate` on the CLI.

## Files

```
flacpress/
├── core.py           # conversion engine (no UI dependencies)
├── app.py             # Flask web server + SSE progress stream
├── cli.py              # terminal front-end
├── static/index.html   # the web UI
├── requirements.txt
└── LICENSE
```

## License

MIT — see [LICENSE](LICENSE).
