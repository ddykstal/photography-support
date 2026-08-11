# photography-support

Command-line tools for working with a Lightroom Classic (LrC) catalog: identifying photos, exporting metadata, preparing images for publishing, and managing catalog backups.

## Scripts

- `lightroom_catalog.py` — Shared helper module for locating and safely copying the `.lrcat` catalog file. Used by the other Python scripts below.
- `show-catalog.py` — Inspect catalog contents (e.g. collections) and list matching photos with metadata identifiers. See `show-catalog.md` for details.
- `add-identifiers.py` — Generate and write unique `dc:Identifier` XMP metadata to photos/sidecars that don't already have one.
- `export-for-fm.py` — Export photo metadata (identifiers, titles, EXIF, rights info) to CSV for use in FileMaker or similar tools.
- `prepare-publish.py` — Prepare copies of images for publishing: resizing/cropping and stripping metadata down to an essential, curated set of tags.
- `lrc-backup-retention.py` / `lrc-backup-retention.rb` — Prune Lightroom Classic catalog backups using a tiered retention policy (daily/weekly/monthly). Defaults to a dry-run; pass `--apply` to actually delete. Equivalent implementations in Python and Ruby.

## Requirements

- Python 3.10+ (for the `.py` scripts) and/or Ruby (for `lrc-backup-retention.rb`)
- [`exiftool`](https://exiftool.org/) on `PATH` for scripts that read/write image metadata
- `sips` (macOS built-in) for image resizing/cropping in `prepare-publish.py`

## Usage

Each script accepts `--help` for detailed usage and options, e.g.:

```bash
python3 show-catalog.py --help
python3 lrc-backup-retention.py --help
ruby lrc-backup-retention.rb --help
```
