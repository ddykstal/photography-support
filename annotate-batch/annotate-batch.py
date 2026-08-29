#!/usr/bin/env python3

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def load_annotator_module(script_path: Path) -> Any:
    module_name = f"{script_path.stem.replace('-', '_')}_dynamic"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {script_path}")
    module = importlib.util.module_from_spec(spec)
    # Python 3.14 dataclasses expect the module to be registered while executing.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def collect_images(upload_dir: Path, patterns: list[str], recursive: bool) -> list[Path]:
    matches: set[Path] = set()
    for pattern in patterns:
        iterator = upload_dir.rglob(pattern) if recursive else upload_dir.glob(pattern)
        for path in iterator:
            if path.is_file():
                matches.add(path)
    return sorted(matches)


def write_manifest_json(manifest_path: Path, payload: dict[str, Any]) -> None:
    parent = manifest_path.parent
    if parent.exists() and not parent.is_dir():
        raise RuntimeError(f"Manifest parent is not a directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    annotate_dir = script_dir.parent / "annotate-border"
    default_profile_path = annotate_dir / "profiles" / "annotation-v2-demo.annotate"

    parser = argparse.ArgumentParser(
        description=(
            "Batch-annotate JPEGs from an upload directory into a download directory "
            "using annotate-border-v2.py profiles."
        ),
        epilog=(
            "Examples:\n"
            "  python3 annotate-batch/annotate-batch.py\n"
            "  python3 annotate-batch/annotate-batch.py --upload-dir ./upload --download-dir ./download --profile annotate-border/profiles/annotation-v2-demo.annotate\n"
            "  python3 annotate-batch/annotate-batch.py --recursive --glob '*.jpg' --glob '*.jpeg' --limit 10\n"
            "  python3 annotate-batch/annotate-batch.py --manifest-json ./download/manifest.json\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--upload-dir",
        default="upload",
        help="Directory containing uploaded images (default: ./upload)",
    )
    parser.add_argument(
        "--download-dir",
        default="download",
        help="Directory where annotated images are written (default: ./download)",
    )
    parser.add_argument(
        "--profile",
        default=str(default_profile_path),
        help="Annotate V2 profile path (default: ../annotate-border/profiles/annotation-v2-demo.annotate)",
    )
    parser.add_argument(
        "--glob",
        dest="globs",
        action="append",
        default=None,
        help="Glob pattern for image selection; repeatable (default: *.jpg, *.jpeg, *.JPG, *.JPEG)",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recurse through subdirectories of upload-dir",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of files to process; 0 means no limit (default: 0)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List planned work without writing output files",
    )
    parser.add_argument(
        "--manifest-json",
        default=None,
        help=(
            "Write run results to a JSON manifest file. "
            "If omitted, no manifest is written."
        ),
    )

    args = parser.parse_args()

    upload_dir = Path(args.upload_dir).expanduser().resolve()
    download_dir = Path(args.download_dir).expanduser().resolve()
    profile_path = Path(args.profile).expanduser().resolve()
    annotate_path = (script_dir.parent / "annotate-border" / "annotate-border-v2.py").resolve()
    manifest_path = Path(args.manifest_json).expanduser().resolve() if args.manifest_json else None

    if not upload_dir.exists() or not upload_dir.is_dir():
        parser.error(f"Upload directory not found: {upload_dir}")
    if not profile_path.exists() or not profile_path.is_file():
        parser.error(f"Profile file not found: {profile_path}")
    if not annotate_path.exists() or not annotate_path.is_file():
        parser.error(f"annotate-border-v2.py not found: {annotate_path}")
    if args.limit < 0:
        parser.error("--limit must be >= 0")

    patterns = args.globs or ["*.jpg", "*.jpeg", "*.JPG", "*.JPEG"]

    annotator = load_annotator_module(annotate_path)
    profile = annotator.parse_profile_v2(profile_path)

    all_images = collect_images(upload_dir, patterns, args.recursive)
    images = all_images[: args.limit] if args.limit > 0 else all_images

    processed = 0
    skipped_existing = 0
    failed = 0
    items: list[dict[str, str]] = []

    for src in images:
        rel = src.relative_to(upload_dir)
        out = download_dir / rel

        if out.exists() and not args.overwrite:
            skipped_existing += 1
            print(f"SKIP (exists): {out}")
            items.append(
                {
                    "input_path": str(src),
                    "output_path": str(out),
                    "relative_path": str(rel),
                    "status": "skipped_exists",
                    "error_message": "",
                }
            )
            continue

        print(f"PROCESS: {src} -> {out}")

        if args.dry_run:
            processed += 1
            items.append(
                {
                    "input_path": str(src),
                    "output_path": str(out),
                    "relative_path": str(rel),
                    "status": "dry_run",
                    "error_message": "",
                }
            )
            continue

        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            annotator.annotate_v2(src, out, profile, diagnostics=False)
            processed += 1
            items.append(
                {
                    "input_path": str(src),
                    "output_path": str(out),
                    "relative_path": str(rel),
                    "status": "processed",
                    "error_message": "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            failed += 1
            message = str(exc)
            print(f"FAILED: {src} ({message})")
            items.append(
                {
                    "input_path": str(src),
                    "output_path": str(out),
                    "relative_path": str(rel),
                    "status": "failed",
                    "error_message": message,
                }
            )

    print("\nSummary")
    print(f"  Selected: {len(images)}")
    print(f"  Processed: {processed}")
    print(f"  Skipped existing: {skipped_existing}")
    print(f"  Failed: {failed}")
    print(f"  Download dir: {download_dir}")

    if manifest_path:
        run_time = datetime.now(UTC)
        manifest = {
            "status_version": 1,
            "run_id": run_time.strftime("%Y%m%dT%H%M%SZ"),
            "timestamp": run_time.isoformat().replace("+00:00", "Z"),
            "upload_dir": str(upload_dir),
            "download_dir": str(download_dir),
            "profile": str(profile_path),
            "recursive": bool(args.recursive),
            "patterns": patterns,
            "limit": int(args.limit),
            "overwrite": bool(args.overwrite),
            "dry_run": bool(args.dry_run),
            "summary": {
                "selected": len(images),
                "processed": processed,
                "skipped_existing": skipped_existing,
                "failed": failed,
            },
            "items": items,
        }
        try:
            write_manifest_json(manifest_path, manifest)
        except Exception as exc:  # noqa: BLE001
            print(f"FAILED: could not write manifest ({exc})")
            return 1
        print(f"  Manifest: {manifest_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
