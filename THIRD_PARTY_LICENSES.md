# Third-party components

FlacPress's own source code is MIT licensed (see [LICENSE](LICENSE)). The
released Windows builds bundle other people's work, and this file records what
and under which terms.

## FFmpeg

The Windows release includes `ffmpeg.exe` and `ffprobe.exe`, taken unmodified
from the [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) "release-essentials"
Windows builds. Those builds are licensed under the **GNU General Public
License v3**.

- Project: <https://ffmpeg.org>
- FFmpeg source: <https://ffmpeg.org/download.html>
- Build scripts used for the Windows binaries: <https://github.com/GyanD/codexffmpeg>
- Licence text: <https://www.gnu.org/licenses/gpl-3.0.html>, and
  `FFMPEG-LICENSE.txt` inside the portable download

FlacPress runs ffmpeg as a separate program (it starts it as a subprocess and
reads its output) rather than linking it into the application, so shipping the
two together is aggregation. FFmpeg remains under the GPL; FlacPress's own code
remains MIT.

## Python libraries

| Library | Licence | Role |
| --- | --- | --- |
| [Flask](https://flask.palletsprojects.com/) | BSD-3-Clause | serves the local web UI |
| Werkzeug, Jinja2, MarkupSafe, itsdangerous, click | BSD-3-Clause | Flask's dependencies |
| [mutagen](https://mutagen.readthedocs.io/) | GPL-2.0-or-later | reads and writes tags and cover art |
| [pywebview](https://pywebview.flowrl.com/) | BSD-3-Clause | native desktop window |
| [pythonnet](https://pythonnet.github.io/) | MIT | pywebview's Windows backend |
| [tqdm](https://tqdm.github.io/) | MPL-2.0 and MIT | CLI progress bar |

## A note on the licence of the built .exe

Worth being aware of, because it's easy to miss: **mutagen is GPL-2.0-or-later,
and unlike ffmpeg it is imported directly into the application**, not run as a
separate program. A binary that combines MIT code with GPL code has to be
distributed under the GPL's terms as a whole.

In practice that means:

- The FlacPress source stays MIT. Anyone can take it under those terms.
- The distributed `.exe`, because mutagen is compiled into it, is effectively
  covered by the GPL. Keeping the source public (it already is) and shipping
  these notices satisfies that.

If you would rather the released binary not be GPL, the only real lever is
replacing mutagen — but that would cost the correct multi-value tag handling and
Ogg `METADATA_BLOCK_PICTURE` cover art that FlacPress deliberately uses it for.
Keeping mutagen and publishing under the GPL is the simpler, more honest trade.
