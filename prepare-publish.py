import argparse
import glob
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from lightroom_catalog import copy_lightroom_catalog_to_temp, resolve_catalog_path




IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".webp",
}

# Source formats that commonly store metadata in .xmp sidecars.
SIDECAR_METADATA_EXTENSIONS = {
    ".cr2",
    ".cr3",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".orf",
    ".rw2",
    ".raf",
    ".pef",
    ".dcr",
    ".kdc",
    ".erf",
    ".3fr",
    ".fff",
    ".iiq",
    ".mos",
    ".mef",
    ".mrw",
    ".raw",
    ".rwl",
    ".x3f",
}

# Tags to preserve after stripping metadata.
ESSENTIAL_TAGS = [
    "XMP-dc:Title",
    "XMP-dc:Description",
    "EXIF:Make",
    "EXIF:Model",
    "EXIF:ISO",
    "EXIF:FNumber",
    "EXIF:ExposureTime",
    "EXIF:FocalLength",
    "EXIF:LensModel",
    "EXIF:DateTimeOriginal",
    "XMP-xmp:CreateDate",
    "XMP-dc:Rights",
    "IPTC:CopyrightNotice",
    "XMP-xmpRights:UsageTerms",
    "XMP-dc:Identifier",
]



@dataclass
class ResizeSpec:
    max_long_edge: int | None = None
    aspect_ratio: tuple[int, int] | None = None


def run_exiftool_json(file_path: Path, tags: list[str]) -> dict[str, object]:
    proc = subprocess.run(
        ["exiftool", "-j", *[f"-{t}" for t in tags], str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"exiftool read failed for {file_path}: {proc.stderr.strip()}")

    import json

    payload = json.loads(proc.stdout)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {}
    return payload[0]


def extract_essential_metadata(file_path: Path) -> dict[str, str]:
    payload = run_exiftool_json(file_path, ESSENTIAL_TAGS)
    extracted: dict[str, str] = {}
    for tag in ESSENTIAL_TAGS:
        short_key = tag.split(":", 1)[1]
        value = payload.get(short_key)
        if value is None:
            value = payload.get(tag)
        if isinstance(value, list):
            value = "; ".join(str(v) for v in value if str(v).strip())
        if value is not None:
            text = str(value).strip()
            if text:
                extracted[tag] = text
    return extracted


def normalize_id_global(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("uuid:"):
        normalized = normalized[5:]
    normalized = normalized.strip("{}")
    return normalized


def strip_and_restore_essential_metadata(file_path: Path, metadata: dict[str, str]) -> None:
    clear_proc = subprocess.run(
        ["exiftool", "-overwrite_original", "-all=", str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if clear_proc.returncode != 0:
        raise RuntimeError(
            f"exiftool clear metadata failed for {file_path}: {clear_proc.stderr.strip()}"
        )

    if not metadata:
        return

    set_args = ["exiftool", "-overwrite_original"]
    for tag, value in metadata.items():
        set_args.append(f"-{tag}={value}")
    set_args.append(str(file_path))

    set_proc = subprocess.run(
        set_args,
        capture_output=True,
        text=True,
        check=False,
    )
    if set_proc.returncode != 0:
        raise RuntimeError(
            f"exiftool restore metadata failed for {file_path}: {set_proc.stderr.strip()}"
        )


def read_sips_dimensions(file_path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sips dimension read failed for {file_path}: {proc.stderr.strip()}")

    width = None
    height = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("pixelWidth:"):
            width = int(line.split(":", 1)[1].strip())
        if line.startswith("pixelHeight:"):
            height = int(line.split(":", 1)[1].strip())

    if width is None or height is None:
        raise RuntimeError(f"Could not parse image dimensions for {file_path}")
    return width, height


def center_crop_to_aspect_ratio(file_path: Path, aspect_ratio: tuple[int, int]) -> None:
    width, height = read_sips_dimensions(file_path)
    target_w, target_h = aspect_ratio

    current_ratio = width / height
    target_ratio = target_w / target_h

    if abs(current_ratio - target_ratio) < 1e-6:
        return

    if current_ratio > target_ratio:
        crop_height = height
        crop_width = int(round(height * target_ratio))
    else:
        crop_width = width
        crop_height = int(round(width / target_ratio))

    proc = subprocess.run(
        ["sips", "--cropToHeightWidth", str(crop_height), str(crop_width), str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sips crop failed for {file_path}: {proc.stderr.strip()}")


def resize_max_long_edge(file_path: Path, max_long_edge: int) -> None:
    width, height = read_sips_dimensions(file_path)
    if max(width, height) <= max_long_edge:
        return

    proc = subprocess.run(
        ["sips", "--resampleHeightWidthMax", str(max_long_edge), str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"sips resize failed for {file_path}: {proc.stderr.strip()}")


def parse_aspect_ratio(value: str) -> tuple[int, int]:
    if ":" in value:
        left, right = value.split(":", 1)
        w = int(left)
        h = int(right)
    else:
        ratio = float(value)
        if ratio <= 0:
            raise ValueError("aspect ratio must be > 0")
        w = int(round(ratio * 1000))
        h = 1000

    if w <= 0 or h <= 0:
        raise ValueError("aspect ratio parts must be positive")
    return (w, h)


def load_resize_spec(target_dir: Path) -> ResizeSpec:
    config_candidates = [target_dir / "publish.yaml", target_dir / "publish.yml"]
    config_path = next((p for p in config_candidates if p.exists()), None)
    if config_path is None:
        return ResizeSpec()

    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PyYAML is required when a YAML resize config is present. "
            "Install with: pip install pyyaml"
        ) from exc

    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML in {config_path}: top level must be a mapping")

    resize = data.get("resize") or data
    if not isinstance(resize, dict):
        raise ValueError(f"Invalid YAML in {config_path}: resize must be a mapping")

    max_long_edge = resize.get("max_long_edge")
    aspect_ratio_raw = resize.get("aspect_ratio")

    parsed_max = None
    if max_long_edge is not None:
        parsed_max = int(max_long_edge)
        if parsed_max < 1:
            raise ValueError("max_long_edge must be >= 1")

    parsed_ratio = None
    if aspect_ratio_raw is not None:
        parsed_ratio = parse_aspect_ratio(str(aspect_ratio_raw))

    return ResizeSpec(max_long_edge=parsed_max, aspect_ratio=parsed_ratio)


def ensure_unique_destination(target_dir: Path, name: str) -> Path:
    candidate = target_dir / name
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        next_candidate = target_dir / f"{stem}__{counter}{suffix}"
        if not next_candidate.exists():
            return next_candidate
        counter += 1




def metadata_target_path_for_source(file_path: Path) -> Path:
    if file_path.suffix.lower() in SIDECAR_METADATA_EXTENSIONS:
        return file_path.with_suffix(".xmp")
    return file_path


def read_dc_identifier_for_source(file_path: Path) -> str | None:
    target = metadata_target_path_for_source(file_path)
    if not target.exists():
        return None

    payload = run_exiftool_json(target, ["XMP-dc:Identifier"])
    value = payload.get("Identifier")
    if value is None:
        value = payload.get("XMP-dc:Identifier")
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def identifier_capture_date(source_identifier: str) -> str | None:
    parts = source_identifier.split("_")
    for part in parts:
        if len(part) == 8 and part.isdigit():
            return f"{part[0:4]}-{part[4:6]}-{part[6:8]}"
    return None


def lookup_catalog_source_info_by_identifier(
    catalog_copy: Path, source_identifier: str
) -> tuple[str, str, str] | None:
    if not source_identifier:
        return None

    capture_date = identifier_capture_date(source_identifier)

    query = """
        SELECT
            rf.absolutePath,
            fo.pathFromRoot,
            f.baseName,
            f.extension,
            i.id_global AS idGlobal,
            i.captureTime
        FROM Adobe_images i
        JOIN AgLibraryFile f ON i.rootFile = f.id_local
        JOIN AgLibraryFolder fo ON f.folder = fo.id_local
        JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
        WHERE fo.pathFromRoot LIKE 'My Photos%'
    """

    params: list[str] = []
    if capture_date:
        query += "\n          AND i.captureTime LIKE ?"
        params.append(f"{capture_date}%")

    with sqlite3.connect(str(catalog_copy)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    for row in rows:
        root = Path((row["absolutePath"] or "").replace("file://", "", 1))
        path_from_root = row["pathFromRoot"] or ""
        base_name = row["baseName"] or ""
        extension = row["extension"] or ""

        filename = f"{base_name}.{extension}" if extension else base_name
        source_path = (root / path_from_root / filename).resolve()
        existing_identifier = read_dc_identifier_for_source(source_path)
        if existing_identifier != source_identifier:
            continue

        source_folder = str(source_path.parent)
        catalog_id_global = normalize_id_global(str(row["idGlobal"] or ""))
        return source_folder, filename, catalog_id_global

    return None


def init_log_db(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publish_file_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                processed_at TEXT NOT NULL,
                id_global TEXT,
                image_location TEXT NOT NULL,
                filename TEXT NOT NULL,
                target_directory TEXT NOT NULL,
                destination_path TEXT NOT NULL
            )
            """
        )
        conn.commit()


def insert_log_row(
    db_path: Path,
    *,
    id_global: str,
    image_location: str,
    filename: str,
    target_directory: str,
    destination_path: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO publish_file_log (
                processed_at,
                id_global,
                image_location,
                filename,
                target_directory,
                destination_path
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                id_global,
                image_location,
                filename,
                target_directory,
                destination_path,
            ),
        )
        conn.commit()


def resolve_input_files(patterns: list[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        for match in matches:
            p = Path(match).expanduser().resolve()
            if not p.exists() or not p.is_file():
                continue
            if p.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if p not in seen:
                seen.add(p)
                found.append(p)

    return sorted(found)


def write_title_caption_text_file(dst_path: Path, metadata: dict[str, str]) -> Path:
    title = metadata.get("XMP-dc:Title", "")
    caption = metadata.get("XMP-dc:Description", "")
    txt_path = dst_path.with_suffix(".txt")
    txt_path.write_text(f"{title}\n{caption}\n", encoding="utf-8")
    return txt_path


def process_one_file(
    src_path: Path,
    target_dir: Path,
    resize_spec: ResizeSpec,
    essential_metadata: dict[str, str],
) -> Path:

    dst_path = ensure_unique_destination(target_dir, src_path.name)
    shutil.copy2(str(src_path), str(dst_path))

    if resize_spec.aspect_ratio:
        center_crop_to_aspect_ratio(dst_path, resize_spec.aspect_ratio)
    if resize_spec.max_long_edge:
        resize_max_long_edge(dst_path, resize_spec.max_long_edge)

    strip_and_restore_essential_metadata(dst_path, essential_metadata)
    write_title_caption_text_file(dst_path, essential_metadata)
    return dst_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare images for publishing: copy to target, apply optional YAML resize/crop, "
            "and strip metadata down to essential fields.\n"
            "\n"
            "Catalog source lookup is strict and uses XMP-dc:Identifier values found in "
            "the source files. Files missing dc:Identifier are skipped."
        ),
        epilog=(
            "Prerequisite:\n"
            "  dc:Identifier values should be assigned first using add-identifiers.py.\n"
            "  This script resolves sources by reading dc:Identifier from the source\n"
            "  files/sidecars referenced by the catalog.\n"
            "\n"
            "YAML config in target directory:\n"
            "  publish.yaml or publish.yml\n"
            "  resize:\n"
            "    max_long_edge: 1500\n"
            "    aspect_ratio: \"4:5\"\n"
            "\n"
            "Examples:\n"
            "  python3 prepare-publish.py '*.jpg'\n"
            "  python3 prepare-publish.py 'exports/**/*.jpg' --target-dir ./publish/instagram\n"
            "  python3 prepare-publish.py '*.jpg' --catalog ~/Pictures/Lightroom/My Catalog.lrcat\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "patterns",
        nargs="+",
        help="Glob-style file patterns to process, e.g. '*.jpg' or 'exports/**/*.jpeg'.",
    )
    parser.add_argument(
        "--target-dir",
        default=str((Path.cwd() / "publish").resolve()),
        help="Destination directory (default: ./publish).",
    )
    parser.add_argument(
        "--log-db",
        default=str((Path.cwd() / "publish-log.sqlite").resolve()),
        help="SQLite log path in current directory (default: ./publish-log.sqlite).",
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help=(
            "Path to Lightroom catalog (.lrcat). If omitted, the newest catalog in "
            "~/Pictures/Lightroom is used."
        ),
    )


    args = parser.parse_args()

    if shutil.which("exiftool") is None:
        parser.error("exiftool is required and was not found in PATH")
    if shutil.which("sips") is None:
        parser.error("sips is required on macOS and was not found in PATH")

    target_dir = Path(args.target_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    log_db = Path(args.log_db).expanduser().resolve()
    init_log_db(log_db)

    try:
        catalog_path = resolve_catalog_path(args.catalog)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    resize_spec = load_resize_spec(target_dir)
    files = resolve_input_files(args.patterns)

    if not files:
        print("No matching image files found.")
        return 0

    print(f"Using catalog: {catalog_path}")
    print(f"Processing {len(files)} files -> {target_dir}")

    with tempfile.TemporaryDirectory(prefix="publish_catalog_") as tmp:
        catalog_copy = copy_lightroom_catalog_to_temp(catalog_path, Path(tmp))

        for src_path in files:
            try:
                essential_metadata = extract_essential_metadata(src_path)
                source_identifier = essential_metadata.get("XMP-dc:Identifier", "").strip()

                if not source_identifier:
                    print(f"SKIP: {src_path} (missing dc:Identifier)")
                    continue

                catalog_source = lookup_catalog_source_info_by_identifier(
                    catalog_copy,
                    source_identifier,
                )
                if catalog_source is None:
                    raise RuntimeError(
                        "No catalog match found for dc:Identifier; "
                        f"dc:Identifier: {source_identifier}"
                    )

                image_location, filename, matched_id_global = catalog_source

                dst_path = process_one_file(
                    src_path,
                    target_dir,
                    resize_spec,
                    essential_metadata,
                )

                insert_log_row(
                    log_db,
                    id_global=matched_id_global,
                    image_location=image_location,
                    filename=filename,
                    target_directory=str(target_dir),
                    destination_path=str(dst_path),
                )
                print(f"OK: {src_path.name} -> {dst_path.name} (catalog match via dc_identifier)")
            except Exception as exc:  # pragma: no cover
                print(f"FAILED: {src_path} ({exc})")

    print(f"Log DB: {log_db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
