<p align="center">
  <img src="static/assets/logo-transparent.png" width="180" alt="FlacPress Logo">
</p>

<h1 align="center">FlacPress</h1>

<p align="center">
Batch convert your lossless music library to high-quality <b>Opus</b>, <b>MP3</b>, or <b>AAC</b> with parallel processing, full metadata preservation, embedded cover art, and a clean local web interface.
</p>

<p align="center">

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.10+-blue)

</p>

---

## Features

- 🎵 Convert **FLAC, WAV, AIFF, ALAC, APE, and WavPack**
- 🚀 Fast parallel conversion using multiple worker processes
- 🎨 Preserves embedded album artwork (including **Opus**)
- 🏷️ Copies metadata using **Mutagen** for maximum compatibility
- 📁 Mirrors your existing folder structure automatically
- 🧠 Detects whether `.m4a` files contain **ALAC** or **AAC**
- 🛑 Real cancellation that immediately terminates running FFmpeg jobs
- ✅ Output verification with FFprobe
- 🧪 Dry-run mode
- 💻 Modern local web UI
- 🖥️ CLI included for terminal users

---

## Supported Formats

| Input | Output |
|-------|--------|
| FLAC | Opus |
| WAV | MP3 |
| AIFF | AAC |
| ALAC (.m4a) | |
| Monkey's Audio (APE) | |
| WavPack (WV) | |

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

---

# Roadmap

- [ ] Drag & Drop support
- [ ] Dark mode
- [ ] Preset profiles
- [ ] ReplayGain support
- [x] Portable releases
- [ ] Automatic update checker

---

# Contributing

Pull requests, feature requests, and bug reports are welcome.

If you encounter an issue, please open an issue on GitHub.

---

# Disclaimer

FlacPress **never modifies your original files**.

All converted audio is written to a separate output directory, keeping your source library untouched.

---

# License

FlacPress's own code is licensed under the MIT License. See the
[LICENSE](LICENSE) file for details.

The released builds also include FFmpeg and other third-party libraries with
their own terms — including one that affects the licence of the distributed
`.exe`. See [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).