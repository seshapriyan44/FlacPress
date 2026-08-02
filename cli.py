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
from enrich import EnrichConfig, EnrichJob

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


def run_job(build_job, show_details: bool = False):
    """Drive a job and report it on the terminal.

    Takes a factory rather than a job because the progress callback has to
    exist before the job does. Works for both ConversionJob and EnrichJob,
    which emit the same events.
    """
    bar = {"pbar": None}

    def on_event(event):
        t = event["type"]
        if t == "scan_done":
            print(f"Found {event['total']} file(s). Working in -> {event['output_dir']}")
            if HAS_TQDM and event["total"]:
                bar["pbar"] = tqdm(total=event["total"], unit="song")
        elif t == "file_done":
            line = None
            if event["status"] == "failed":
                line = f"FAILED  {event['file']}  {event.get('detail', '')}"
            elif show_details and event.get("detail") and event["status"] in ("dry", "fixed"):
                line = f"{event['status'].upper():>6}  {event['file']}\n"\
                       f"        {event['detail']}"
            if bar["pbar"] is not None:
                bar["pbar"].update(1)
                if line:
                    bar["pbar"].write(line)
            elif line:
                print(line)
        elif t == "warning":
            message = f"NOTE: {event['detail']}"
            (bar["pbar"].write(message) if bar["pbar"] is not None else print(message))
        elif t == "error":
            print("ERROR:", event["detail"])
        elif t == "job_done":
            if bar["pbar"] is not None:
                bar["pbar"].close()
            print("\n" + "=" * 50)
            print("Finished" + (" (cancelled)" if event.get("cancelled") else ""))
            print("=" * 50)
            for key, value in event["stats"].items():
                print(f"{key:>14}: {value}")

    job = build_job(on_event)
    try:
        job.run()
    except KeyboardInterrupt:
        job.cancel()
        print("\nCancelling… waiting for in-flight files to stop.")


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
                              "128k/192k/256k for aac, c5/c8/c12 for flac, "
                              "s16/s24 for wav and aiff. Omit for the "
                              "recommended default.")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-convert even if output already exists")
    parser.add_argument("--no-verify", action="store_true", help="Skip ffprobe verification of outputs")
    parser.add_argument("--no-skip-lossy-m4a", action="store_true",
                         help="Also re-encode .m4a files that are already lossy AAC")

    fixing = parser.add_argument_group(
        "filling in missing information",
        "Looks up whatever a file is missing: tags, cover art and synced "
        "lyrics. Existing information is never overwritten.")
    fixing.add_argument("--fix-only", action="store_true",
                        help="Don't convert anything - just fix the files where "
                             "they are. Previews the changes unless --apply is given.")
    fixing.add_argument("--apply", action="store_true",
                        help="With --fix-only, actually write the changes")
    fixing.add_argument("--fix-metadata", action="store_true",
                        help="Fill in missing title/artist/album/track/year/genre")
    fixing.add_argument("--fix-art", action="store_true",
                        help="Find and embed missing cover art")
    fixing.add_argument("--fix-lyrics", action="store_true",
                        help="Find and embed missing synced (.lrc) lyrics")
    fixing.add_argument("--fix-all", action="store_true",
                        help="Shorthand for --fix-metadata --fix-art --fix-lyrics")
    fixing.add_argument("--lrc-sidecar", action="store_true",
                        help="Also write lyrics as a .lrc file next to the track")
    fixing.add_argument("--offline", action="store_true",
                        help="No internet lookups: use only file names and any "
                             "cover image already in the folder")
    args = parser.parse_args()

    if not args.source.is_dir():
        parser.error(f"{args.source} is not a directory")

    fix_metadata = args.fix_metadata or args.fix_all
    fix_art = args.fix_art or args.fix_all
    fix_lyrics = args.fix_lyrics or args.fix_all

    if args.fix_only:
        if not (fix_metadata or fix_art or fix_lyrics):
            # Asking to fix nothing in particular means fix everything.
            fix_metadata = fix_art = fix_lyrics = True
        cfg = EnrichConfig(
            source_dir=args.source,
            fix_metadata=fix_metadata,
            fix_art=fix_art,
            fix_lyrics=fix_lyrics,
            use_online=not args.offline,
            lrc_sidecar=args.lrc_sidecar,
            workers=args.workers or max(1, min(4, (os.cpu_count() or 4) - 2)),
            dry_run=not args.apply,
        )
        if cfg.dry_run:
            print("Preview only - nothing will be written. Add --apply to commit.")
        run_job(lambda emit: EnrichJob(cfg, on_event=emit), show_details=True)
        return

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
        fix_missing_metadata=fix_metadata,
        fix_missing_art=fix_art,
        fix_missing_lyrics=fix_lyrics,
        online_lookups=not args.offline,
    )

    run_job(lambda emit: ConversionJob(cfg, on_event=emit))


if __name__ == "__main__":
    main()
