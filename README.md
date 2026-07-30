<p align="center">
  <img src="static/assets/logo-transparent.png" width="180" alt="FlacPress Logo">
</p>

<h1 align="center">FlacPress</h1>

<p align="center">
Batch convert your lossless music library to high-quality <b>Opus</b>, <b>MP3</b> or <b>AAC</b> — or repackage it losslessly as <b>FLAC</b>, <b>ALAC</b>, <b>WAV</b> or <b>AIFF</b> — with parallel processing, full metadata preservation, embedded cover art, and a clean local web interface.
</p>

<p align="center">
It also fixes libraries: FlacPress can fill in <b>missing tags</b>, find <b>missing album art</b> and embed <b>missing synced lyrics</b>, either while converting or on your existing files without converting anything.
</p>

<p align="center">

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.10+-blue)

</p>

---

## Features

**Converting**

- 🎵 Reads **FLAC, WAV, AIFF, ALAC, APE, WavPack, TTA, TAK, DSF, Shorten** and more
- 📦 Writes lossy (**Opus, MP3, AAC**) or lossless (**FLAC, ALAC, WAV, AIFF**)
- 🚀 Fast parallel conversion across multiple workers
- 🎨 Carries embedded album artwork across every format, including Opus
- 🏷️ Copies metadata using **Mutagen** for maximum compatibility
- 💬 Carries embedded lyrics across too
- 📁 Mirrors your existing folder structure automatically
- 🧠 Detects whether `.m4a` files contain **ALAC** or **AAC**
- 🛑 Real cancellation that immediately terminates running FFmpeg jobs
- ✅ Output verification with FFprobe
- 🧪 Dry-run mode

**Fixing what's missing** — while converting, or on its own

- 🏷️ Fills in absent **title, artist, album, album artist, track number, year and genre**
- 🖼️ Finds and embeds **missing cover art**
- 🎤 Finds and embeds **missing synced lyrics** (timestamped `.lrc`)
- 📂 Reads your **file and folder names** first, so it works with no internet
- 🔒 Only ever fills in blanks — existing tags are never overwritten
- 👀 Previews every change before writing anything

**And**

- 💻 Modern local web UI
- 🖥️ CLI included for terminal users
- 📴 Everything runs on your machine; only the optional lookups touch the network

---

## Supported Formats

**Input** — anything lossless that FFmpeg can decode:

FLAC · WAV · AIFF / AIFC · ALAC (.m4a) · Monkey's Audio (APE) · WavPack (WV) ·
TTA · TAK · Shorten (SHN) · DSD (DSF / DFF) · CAF · Sony Wave64 (W64) · MLP

**Output**

| Compressed | Lossless |
|-----------|----------|
| Opus — best quality per megabyte | FLAC — the standard, widely supported |
| MP3 — plays on everything | ALAC — for iPhone, iTunes, Apple Music |
| AAC (.m4a) — better than MP3 at the same size | WAV — uncompressed, for editing software |
| | AIFF — uncompressed, the Mac equivalent |

Lossless output doesn't shrink anything worth mentioning — it's there for when
you need a *different* format rather than a smaller one, like getting a FLAC
library onto an iPhone as ALAC.

---

## Fixing missing tags, art and lyrics

Pick **Fix library** in the app (or `--fix-only` on the command line) to repair
files where they are, without converting. The same options are available during
a conversion, where they apply to the converted copy instead.

**Where the information comes from**, in this order:

1. **What the file already has.** Kept as-is, always. Nothing is overwritten.
2. **A cover image already in the album folder** (`cover.jpg`, `folder.png`, …).
   Free, instant, and already your choice.
3. **An online lookup.** [iTunes](https://performance-partners.apple.com/search-api)
   → [Deezer](https://developers.deezer.com/api) →
   [MusicBrainz](https://musicbrainz.org) +
   [Cover Art Archive](https://coverartarchive.org) for art and tags, and
   [LRCLIB](https://lrclib.net) for synced lyrics. No API keys, no accounts.
4. **Your file and folder names.** `Daft Punk/2001 - Discovery/01 - One More Time.flac`
   already contains an artist, a year, an album, a track number and a title.
   This works with no internet at all.

**Safety.** Fixing edits your files, so it previews by default: you get a full
list of exactly what would be added, then decide. Only the tag block is
rewritten — the audio itself is never re-encoded, so there's no quality loss.
A field that already has a value is left alone.

Lyrics are matched on track length as well as name, so a five-minute album
version doesn't get timings from a three-minute radio edit. Wrong lyrics are
worse than none.

---

## Screenshot

![alt text](image.png)


---

# Download (Windows)

Grab the latest build from the [Releases page](https://github.com/seshapriyan44/FlacPress/releases):

| File | What it is |
| --- | --- |
| `FlacPress.exe` | One file. Download, double-click, done. |
| `FlacPress-portable-windows.zip` | Unzip and run `FlacPress.exe` from the folder. Starts a little faster. |

**FFmpeg is already inside both downloads.** There is nothing else to install.

Two things to expect on first run:

- Windows may show *"Windows protected your PC"* because the build isn't
  code-signed (signing certificates cost money). Click **More info -> Run
  anyway**.
- The single-file version unpacks itself to a temporary folder each time it
  starts, so give it a few seconds. The portable version skips that step.

Everything below is for running from source or building it yourself.

---

# Installation (from source)

### Clone the repository

```bash
git clone https://github.com/seshapriyan44/FlacPress.git
cd FlacPress
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Install FFmpeg

FlacPress requires **FFmpeg** and **FFprobe**. (Only when running from source —
the released builds already include them.)

FlacPress looks for them in this order, so any one of these works:

1. bundled inside the build (how the releases ship)
2. a `bin/` folder next to `FlacPress.exe`, or the two files sitting directly
   beside it
3. anywhere on your `PATH`

Verify the installation:

```bash
ffmpeg -version
ffprobe -version
```

Downloads:

https://ffmpeg.org/download.html

---

## Quick Start

Launch the web application:

```bash
python app.py
```

Open

```
http://127.0.0.1:5000
```

If port 5000 is already in use, FlacPress picks the next free one and prints the
address to use instead.

Choose:

- Source folder
- Output folder (optional)
- Output format
- Bitrate
- Number of workers

Click **Start Conversion**.

---

# CLI Usage

Convert to Opus:

```bash
python cli.py ~/Music --format opus --bitrate 160k --workers 6
```

Dry run:

```bash
python cli.py ~/Music --format mp3 --bitrate 0 --dry-run
```

Windows example:

```bash
python cli.py D:\Music --format opus
```

Repackage a FLAC library as ALAC for an iPhone, nothing lost:

```bash
python cli.py D:\Music --format alac --destination D:\ForPhone
```

### Fixing files instead of converting them

Preview what's missing across a library — writes nothing:

```bash
python cli.py D:\Music --fix-only
```

Apply it:

```bash
python cli.py D:\Music --fix-only --apply
```

Just the album art, and also save lyrics as `.lrc` files alongside the tracks:

```bash
python cli.py D:\Music --fix-only --fix-art --apply
python cli.py D:\Music --fix-only --fix-lyrics --lrc-sidecar --apply
```

No internet — use only file names and any cover image already in the folder:

```bash
python cli.py D:\Music --fix-only --offline --apply
```

Fill in the gaps while converting (applies to the converted copy, not the
original):

```bash
python cli.py D:\Music --format opus --fix-all
```

---

# Desktop Version

FlacPress can also run as a native desktop application using **PyWebView**.

Run:

```bash
python desktop.py
```

---

# Building the Windows app

## The easy way: let GitHub build it

[`.github/workflows/build-windows.yml`](.github/workflows/build-windows.yml)
builds the app on a Windows runner on every push. Open the **Actions** tab, pick
the newest run, and download the `FlacPress-windows` artifact — it contains both
the single-file exe and the portable zip.

To publish a release, push a tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The workflow then attaches both downloads to a GitHub Release automatically.
Before packaging it also converts a generated test file on Windows, so a build
that breaks the engine fails instead of shipping.

## Building it yourself on Windows

```bash
pip install -r requirements.txt
```

Put `ffmpeg.exe` and `ffprobe.exe` in a `bin/` folder to bundle them
(recommended — otherwise users need ffmpeg installed):

```
FlacPress/
└── bin/
    ├── ffmpeg.exe
    └── ffprobe.exe
```

Then build:

```bash
pyinstaller flacpress.spec
```

That produces `dist/FlacPress.exe`. For the folder version, which starts faster
because it doesn't unpack on every launch:

```bash
set FLACPRESS_ONEFILE=0
pyinstaller flacpress.spec
```

[`flacpress.spec`](flacpress.spec) handles the icon, the bundled `static/`
files, the ffmpeg binaries, and pywebview's platform backend. Building
`desktop.py` by hand with command-line flags misses some of those.

---

# Why FlacPress?

Most lossless-to-lossy converters are either simple shell scripts or heavyweight desktop applications.

FlacPress focuses on being:

- Lightweight
- Fast
- Easy to use
- Transparent
- Safe

Instead of hiding everything behind a progress bar, every worker is displayed live so you can monitor conversions in real time.

---

# Design Highlights

### Metadata Preservation

Rather than relying on FFmpeg's `-map_metadata`, FlacPress uses **Mutagen** to preserve metadata more reliably across every supported format.

---

### Embedded Cover Art

Album artwork is preserved for:

- Opus
- MP3
- AAC

For Opus, FlacPress writes a proper `METADATA_BLOCK_PICTURE` tag after encoding.

---

### Smart `.m4a` Detection

`.m4a` may contain either:

- ALAC (lossless)
- AAC (lossy)

FlacPress automatically detects the codec and avoids unnecessary lossy-to-lossy transcoding.

---

### Safe Output Handling

Generated files are never rescanned as input, even when the destination folder is inside the source directory.

---

### Real Cancellation

Cancel immediately terminates active FFmpeg processes instead of waiting for the conversion queue to finish.

---

### Output Verification

Every converted file is verified using FFprobe before being marked as successful.

---

### UI-Independent Engine

The conversion engine is completely independent of the user interface.

The same backend powers:

- Flask Web UI
- CLI
- Desktop application

making future interfaces easy to build.

The library fixer follows the same rule. It's a second job type that emits the
same progress events as a conversion, so the web UI, the terminal output and
the streaming endpoint all drive it without knowing which one is running.

### One Tag Layer, Every Container

Each format hides the same information somewhere different: Vorbis comments and
picture blocks in FLAC and Ogg, ID3 frames in MP3, WAV and AIFF, four-character
atoms in MP4 for AAC and ALAC. `tagging.py` puts one interface over all of them
so the converter and the fixer share a single implementation.

That layer is also why FFmpeg is no longer asked to carry cover art. Artwork
used to be stream-copied during the encode, which meant a source image the
target container disliked could fail the whole file and need a retry without it.
Now FFmpeg handles audio and Mutagen handles everything else — which it had to
do for FLAC, Ogg, WAV and AIFF anyway.

### Conservative Lookups

Guessing wrong is worse than not guessing:

- Nothing that already has a value is ever overwritten.
- Online matches are scored on artist, title, album and track length, and
  rejected below a confidence threshold rather than accepting the closest hit.
- Lyrics must match on duration as well as name, so an album version can't
  inherit timings from a radio edit.
- Art is cached per album, so a twelve-track album costs one lookup.
- Requests are rate-limited per service, and a few connection failures in a row
  stop the lookups entirely rather than making 500 files wait for 500 timeouts.

---

# Default Quality Settings

| Format | Default |
|----------|---------|
| Opus | 160 kbps |
| MP3 | LAME V0 |
| AAC | 192 kbps |

All settings can be overridden from either the UI or CLI.

---

# Project Structure

```
FlacPress/
├── app.py                  Flask web UI
├── cli.py                  terminal front-end
├── desktop.py              native window (and the PyInstaller entry point)
├── core.py                 the conversion engine
├── enrich.py               the library fixer, and filename guessing
├── providers.py            online lookups for art, lyrics and tags
├── tagging.py              reads/writes tags, art and lyrics in any container
├── runtime.py              paths, tool discovery, packaging support
├── flacpress.spec          PyInstaller build recipe
├── static/
├── bin/                    optional: ffmpeg.exe + ffprobe.exe to bundle
├── .github/workflows/      Windows build + release automation
├── requirements.txt
└── LICENSE
```

---

# Requirements

- Python 3.10+
- FFmpeg
- FFprobe
- Mutagen — required, not optional: all tag, cover art and lyrics handling
  goes through it
- An internet connection **only** if you want the online lookups. Everything
  else, including guessing tags from file names, works offline.

---

# Roadmap

- [ ] Drag & Drop support
- [ ] Dark mode
- [ ] Preset profiles
- [ ] ReplayGain support
- [x] Portable releases
- [ ] Automatic update checker
- [x] Lossless output formats (FLAC, ALAC, WAV, AIFF)
- [x] Automatic cover art lookup
- [x] Automatic synced lyrics (.lrc)
- [x] Fill in missing tags
- [x] Standalone library fixer (no conversion needed)
- [ ] Acoustic fingerprinting for files with no usable names or tags

---

# Contributing

Pull requests, feature requests, and bug reports are welcome.

If you encounter an issue, please open an issue on GitHub.

---

# What FlacPress does to your files

**Converting never touches your originals.** Converted audio always goes to a
separate output folder. The source library is only ever read from.

**Fixing your library does modify your files** — that is what it's for. Worth
being precise about what that means:

- **Only the tag block is rewritten.** The audio stream is left completely
  alone, so nothing is re-encoded and no quality is lost.
- **Only blanks get filled.** A title, cover image or set of lyrics that's
  already there is never replaced.
- **It previews by default.** You see every proposed change, then press
  **Apply** (or pass `--apply`) before a single byte is written.
- **It isn't undoable.** If you're unsure, run the preview and try one album
  before pointing it at your whole collection.

Tags are guessed from folder names when they're missing, and folder names
aren't always right. The preview exists so you can catch that before it's
written rather than after.

---

# License

FlacPress's own code is licensed under the MIT License. See the
[LICENSE](LICENSE) file for details.

The released builds also include FFmpeg and other third-party libraries with
their own terms — including one that affects the licence of the distributed
`.exe`. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).