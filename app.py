"""
FlacPress — local web UI for the batch audio converter.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import os
import queue
import string
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import providers
from core import (
    ConversionJob, FORMAT_PRESETS, JobConfig, check_binaries, is_lossless_output,
)
from enrich import EnrichConfig, EnrichJob
from runtime import free_port, resource_path

# static_folder has to be an absolute path. Flask normally works it out
# relative to this file, but in a PyInstaller build the UI files are
# unpacked into a temporary folder at launch, so that guess lands
# somewhere that doesn't exist and every page 404s.
app = Flask(__name__, static_folder=str(resource_path("static")), static_url_path="")

JOBS: dict[str, dict] = {}  # job_id -> {"job": ConversionJob|EnrichJob, "events": Queue}


def _launch(build_job) -> str:
    """Start a job on a background thread and wire it to an event queue.

    Both job types expose the same run()/cancel() pair and emit the same
    event shape, so the streaming and cancel endpoints don't care which one
    they're talking to.
    """
    events: "queue.Queue[dict]" = queue.Queue()
    job = build_job(events.put)
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"job": job, "events": events}

    def _run():
        try:
            job.run()
        finally:
            events.put({"type": "stream_end"})

    threading.Thread(target=_run, daemon=True).start()
    return job_id


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/check")
def api_check():
    missing = check_binaries()
    presets = {
        key: {
            "ext": p["ext"],
            "embed_cover": p["embed_cover"],
            "lossless": is_lossless_output(key),
            "bitrate_options": p["bitrate_options"],
        }
        for key, p in FORMAT_PRESETS.items()
    }
    return jsonify({"ok": not missing, "missing": missing, "presets": presets,
                     "cpu_count": os.cpu_count() or 4,
                     "network": providers.status()})


@app.route("/api/drives")
def api_drives():
    if os.name == "nt":
        drives = [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    else:
        drives = ["/"]
    return jsonify({"drives": drives, "home": str(Path.home())})


@app.route("/api/browse")
def api_browse():
    raw = request.args.get("path") or str(Path.home())
    p = Path(raw)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": "Not a directory"}), 400
    try:
        dirs = sorted(
            (c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")),
            key=str.lower,
        )
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    parent = str(p.parent) if p.parent != p else None
    return jsonify({"path": str(p), "parent": parent, "dirs": dirs})


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True) or {}
    raw_source = (data.get("source_dir") or "").strip()
    if not raw_source:
        return jsonify({"error": "Source directory is required"}), 400

    source_dir = Path(raw_source).expanduser()
    if not source_dir.exists() or not source_dir.is_dir():
        return jsonify({"error": f"'{raw_source}' is not a directory"}), 400

    fmt = data.get("format", "opus")
    if fmt not in FORMAT_PRESETS:
        return jsonify({"error": f"Unsupported format '{fmt}'"}), 400

    raw_dest = (data.get("destination_dir") or "").strip()
    destination_dir = None
    if raw_dest:
        destination_dir = Path(raw_dest).expanduser()
        if destination_dir.exists() and not destination_dir.is_dir():
            return jsonify({"error": f"'{raw_dest}' is not a directory"}), 400

    try:
        workers = max(1, min(int(data.get("workers", 4)), 32))
    except (TypeError, ValueError):
        return jsonify({"error": "workers must be an integer"}), 400

    cfg = JobConfig(
        source_dir=source_dir,
        destination_dir=destination_dir,
        output_format=fmt,
        bitrate=(data.get("bitrate") or "").strip() or None,
        workers=workers,
        dry_run=bool(data.get("dry_run", False)),
        force=bool(data.get("force", False)),
        verify=bool(data.get("verify", True)),
        skip_lossy_m4a=bool(data.get("skip_lossy_m4a", True)),
        embed_cover_art=bool(data.get("embed_cover_art", True)),
        fix_missing_metadata=bool(data.get("fix_missing_metadata", False)),
        fix_missing_art=bool(data.get("fix_missing_art", False)),
        fix_missing_lyrics=bool(data.get("fix_missing_lyrics", False)),
        online_lookups=bool(data.get("online_lookups", True)),
    )

    job_id = _launch(lambda emit: ConversionJob(cfg, on_event=emit))
    return jsonify({
        "job_id": job_id,
        "output_dir": str(destination_dir or source_dir.parent),
        "workers": workers,
    })


@app.route("/api/fix/start", methods=["POST"])
def api_fix_start():
    """Run the library fixer on files where they are - no conversion.

    Previews by default: dry_run has to be turned off explicitly, because
    unlike converting, this edits the user's own files.
    """
    data = request.get_json(force=True) or {}
    raw_source = (data.get("source_dir") or "").strip()
    if not raw_source:
        return jsonify({"error": "Source directory is required"}), 400

    source_dir = Path(raw_source).expanduser()
    if not source_dir.exists() or not source_dir.is_dir():
        return jsonify({"error": f"'{raw_source}' is not a directory"}), 400

    try:
        workers = max(1, min(int(data.get("workers", 4)), 16))
    except (TypeError, ValueError):
        return jsonify({"error": "workers must be an integer"}), 400

    fix_metadata = bool(data.get("fix_metadata", True))
    fix_art = bool(data.get("fix_art", True))
    fix_lyrics = bool(data.get("fix_lyrics", True))
    if not (fix_metadata or fix_art or fix_lyrics):
        return jsonify({"error": "Pick at least one thing to fix"}), 400

    cfg = EnrichConfig(
        source_dir=source_dir,
        fix_metadata=fix_metadata,
        fix_art=fix_art,
        fix_lyrics=fix_lyrics,
        use_online=bool(data.get("use_online", True)),
        infer_from_names=bool(data.get("infer_from_names", True)),
        use_local_cover=bool(data.get("use_local_cover", True)),
        lrc_sidecar=bool(data.get("lrc_sidecar", False)),
        art_size=int(data.get("art_size", 1000) or 1000),
        recursive=bool(data.get("recursive", True)),
        workers=workers,
        dry_run=bool(data.get("dry_run", True)),
    )

    job_id = _launch(lambda emit: EnrichJob(cfg, on_event=emit))
    return jsonify({"job_id": job_id, "output_dir": str(cfg.source_dir),
                     "workers": workers, "dry_run": cfg.dry_run})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    entry = JOBS.get(job_id)
    if not entry:
        return jsonify({"error": "Unknown job"}), 404
    entry["job"].cancel()
    return jsonify({"ok": True})


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    entry = JOBS.get(job_id)
    if not entry:
        return jsonify({"error": "Unknown job"}), 404

    def gen():
        events = entry["events"]
        while True:
            event = events.get()
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("type") == "stream_end":
                JOBS.pop(job_id, None)
                break

    return Response(gen(), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = free_port(5000)
    print(f"FlacPress is running — open http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
