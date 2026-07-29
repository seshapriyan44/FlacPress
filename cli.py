"""
Terminal front-end for core.py — closest thing to the original script,
but using the fixed/optimized engine underneath.

Usage:
    python cli.py "E:\\Songs\\Some Album" --format opus --bitrate 160k --workers 6
    python cli.py "E:\\Songs\\English\\The Weeknd" --destination "D:\\Converted" --format mp3
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from core import ConversionJob, FORMAT_PRESETS, JobConfig

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


def main():
    parser = argparse.ArgumentParser(description="Batch-convert lossless audio to a compressed format.")
    parser.add_argument("source", type=Path, help="Source folder to scan recursively")
    parser.add_argument("--destination", type=Path, default=None,
                         help="Where converted files go, mirroring the source's folder "
                              "structure (only the album folder gets the format name "
                              "appended). Defaults to the source folder's parent.")
    parser.add_argument("--format", choices=FORMAT_PRESETS.keys(), default="opus")
    parser.add_argument("--bitrate", default=None,
                         help="Bitrate/quality preset id for the chosen format, e.g. "
                              "160k/128k/192k for opus, v0/v2/v4/320k for mp3, "
                              "128k/192k/256k for aac. Omit for the recommended default.")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-convert even if output already exists")
    parser.add_argument("--no-verify", action="store_true", help="Skip ffprobe verification of outputs")
    parser.add_argument("--no-skip-lossy-m4a", action="store_true",
                         help="Also re-encode .m4a files that are already lossy AAC")
    args = parser.parse_args()

    if not args.source.is_dir():
        parser.error(f"{args.source} is not a directory")

    cfg = JobConfig(
        source_dir=args.source,
        destination_dir=args.destination,
        output_format=args.format,
        bitrate=args.bitrate,
        workers=args.workers or max(1, (os.cpu_count() or 4) - 2),
        dry_run=args.dry_run,
        force=args.force,
        verify=not args.no_verify,
        skip_lossy_m4a=not args.no_skip_lossy_m4a,
    )

    bar = {"pbar": None}

    def on_event(event):
        t = event["type"]
        if t == "scan_done":
            print(f"Found {event['total']} lossless file(s). Output -> {event['output_dir']}")
            if HAS_TQDM:
                bar["pbar"] = tqdm(total=event["total"], unit="song")
        elif t == "file_done" and bar["pbar"] is not None:
            bar["pbar"].update(1)
            if event["status"] == "failed":
                bar["pbar"].write(f"FAILED  {event['file']}  {event.get('detail','')}")
        elif t == "error":
            print("ERROR:", event["detail"])
        elif t == "job_done":
            if bar["pbar"] is not None:
                bar["pbar"].close()
            s = event["stats"]
            print("\n" + "=" * 50)
            print("Finished" + (" (cancelled)" if event.get("cancelled") else ""))
            print("=" * 50)
            for k, v in s.items():
                print(f"{k:>14}: {v}")

    job = ConversionJob(cfg, on_event=on_event)
    try:
        job.run()
    except KeyboardInterrupt:
        job.cancel()
        print("\nCancelling… waiting for in-flight files to stop.")


if __name__ == "__main__":
    main()
