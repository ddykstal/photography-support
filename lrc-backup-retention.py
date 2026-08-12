#!/usr/bin/env python3
"""
Lightroom Classic Backups retention script.

Keeps:
- Latest backup per day (collapsing multiple runs per day)
- Last 5 daily backups (5 most recent backup-days)
- Last 0 weekly backups (disabled by default)
- Last 0 monthly backups (disabled by default)

Default: dry-run (prints what it would delete).
Use --apply to actually delete.

Typical Lightroom backup structure:
Backups/
  YYYY-MM-DD HHMM/
    <catalog>.lrcat.zip
"""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


DEFAULT_BACKUPS_ROOT = Path("~/Pictures/Lightroom/Backups").expanduser()


FOLDER_DT_PATTERNS = [
    # Common LrC formats
    ("%Y-%m-%d %H%M", re.compile(r"^\d{4}-\d{2}-\d{2} \d{4}$")),
    ("%Y-%m-%d %H-%M-%S", re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}$")),
    ("%Y-%m-%d_%H%M", re.compile(r"^\d{4}-\d{2}-\d{2}_\d{4}$")),
]


@dataclass(frozen=True)
class Backup:
    folder: Path           # backup directory (child of Backups/)
    dt: datetime           # timestamp for retention grouping (best guess)
    zip_files: tuple[Path, ...]


def parse_backup_datetime(folder: Path) -> datetime:
    """
    Best-effort timestamp:
    1) Parse folder name like 'YYYY-MM-DD HHMM'
    2) Else use folder mtime
    """
    name = folder.name
    for fmt, rx in FOLDER_DT_PATTERNS:
        if rx.match(name):
            try:
                return datetime.strptime(name, fmt)
            except ValueError:
                pass

    # fallback: folder mtime
    return datetime.fromtimestamp(folder.stat().st_mtime)


def find_backups(backups_root: Path) -> list[Backup]:
    backups: list[Backup] = []
    if not backups_root.exists():
        raise FileNotFoundError(f"Backups folder not found: {backups_root}")

    for child in backups_root.iterdir():
        if not child.is_dir():
            continue
        zips = tuple(sorted(child.glob("*.zip")))
        # Lightroom backups are almost always zips; skip empty/non-backup dirs
        if not zips:
            continue
        dt = parse_backup_datetime(child)
        backups.append(Backup(folder=child, dt=dt, zip_files=zips))

    backups.sort(key=lambda b: b.dt, reverse=True)  # newest first
    return backups


def latest_per_key(backups: Iterable[Backup], key_fn) -> dict[object, Backup]:
    """
    Given backups (preferably sorted newest-first), return newest backup per key.
    """
    latest: dict[object, Backup] = {}
    for b in backups:
        k = key_fn(b)
        if k not in latest:
            latest[k] = b
    return latest


def retention_set(backups: list[Backup], keep_days: int, keep_weeks: int, keep_months: int) -> set[Path]:
    """
    Return a set of backup folders to KEEP, per policy.
    """
    # 1) Collapse multiple backups per day: keep latest of each day
    daily_latest = latest_per_key(backups, lambda b: b.dt.date())
    daily_latest_list = sorted(daily_latest.values(), key=lambda b: b.dt, reverse=True)

    keep: set[Path] = set()

    # Always keep the latest backup per day (implicitly via our selections below),
    # but we still need to decide WHICH days to keep.
    # 2) Keep last N daily backups (most recent backup-days)
    recent_daily = daily_latest_list[:keep_days]
    keep.update(b.folder for b in recent_daily)

    # Remaining older daily backups
    older_daily = daily_latest_list[keep_days:]

    # 3) Keep last N weekly backups among older daily backups
    # Use ISO week (year, week) so weeks are stable across years.
    weekly_latest = latest_per_key(older_daily, lambda b: b.dt.isocalendar()[:2])  # (iso_year, iso_week)
    weekly_latest_list = sorted(weekly_latest.values(), key=lambda b: b.dt, reverse=True)
    keep_weekly = weekly_latest_list[:keep_weeks]
    keep.update(b.folder for b in keep_weekly)

    # Remove ones already kept, then do monthlies from the remainder
    older_after_weekly = [b for b in older_daily if b.folder not in keep]

    # 4) Keep last N monthly backups among what remains
    monthly_latest = latest_per_key(older_after_weekly, lambda b: (b.dt.year, b.dt.month))
    monthly_latest_list = sorted(monthly_latest.values(), key=lambda b: b.dt, reverse=True)
    keep_monthly = monthly_latest_list[:keep_months]
    keep.update(b.folder for b in keep_monthly)

    return keep


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune Lightroom Classic catalog backups with a tiered retention policy.")
    ap.add_argument(
        "backups_root",
        nargs="?",
        type=Path,
        default=DEFAULT_BACKUPS_ROOT,
        help=f"Path to Lightroom 'Backups' folder (default: {DEFAULT_BACKUPS_ROOT})",
    )
    ap.add_argument("--keep-days", type=int, default=5, help="Number of daily backups to keep (default: 5)")
    ap.add_argument("--keep-weeks", type=int, default=0, help="Number of weekly backups to keep (default: 0)")
    ap.add_argument("--keep-months", type=int, default=0, help="Number of monthly backups to keep (default: 0)")
    ap.add_argument("--apply", action="store_true", help="Actually delete; otherwise dry-run")
    args = ap.parse_args()

    backups = find_backups(args.backups_root)
    if not backups:
        print(f"No backup folders with .zip files found under: {args.backups_root}")
        return 0

    keep_folders = retention_set(
        backups,
        keep_days=args.keep_days,
        keep_weeks=args.keep_weeks,
        keep_months=args.keep_months,
    )

    to_delete = [b for b in backups if b.folder not in keep_folders]

    print(f"Found backups: {len(backups)}")
    print(f"Keeping:       {len(keep_folders)} folders")
    print(f"Deleting:      {len(to_delete)} folders")
    print()

    # Print what we keep (optional but useful)
    print("KEEP:")
    for b in sorted([b for b in backups if b.folder in keep_folders], key=lambda x: x.dt, reverse=True):
        print(f"  {b.dt:%Y-%m-%d %H:%M}  {b.folder.name}")

    print("\nDELETE:" if to_delete else "\nDELETE: (none)")
    for b in to_delete:
        print(f"  {b.dt:%Y-%m-%d %H:%M}  {b.folder.name}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to delete.")
        return 0

    print("\nApplying deletions...")
    for b in to_delete:
        # Delete the entire backup folder
        shutil.rmtree(b.folder)
        print(f"Deleted {b.folder}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())