import argparse
import json
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path

from lightroom_catalog import copy_lightroom_catalog_to_temp, resolve_catalog_path

# Base62 alphabet (filename-safe): A-Z, a-z, 0-9.
BASE62_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"

# Common still-image formats where metadata is usually written directly into the file.
IN_FILE_METADATA_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".png",
    ".psd",
    ".dng",
    ".heic",
    ".heif",
}

# Common RAW formats that typically use XMP sidecars for metadata workflows.
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

# Video formats should be skipped for this metadata identifier flow.
VIDEO_EXTENSIONS = {
    ".mov",
    ".mp4",
    ".m4v",
    ".avi",
    ".mkv",
    ".mts",
    ".m2ts",
    ".wmv",
    ".flv",
    ".webm",
    ".mpg",
    ".mpeg",
}


def parse_creation_date(capture_time: object | None) -> str | None:
    """Parse Lightroom captureTime into YYYYMMDD, or return None if unavailable/invalid."""
    if not capture_time:
        return None

    raw = str(capture_time)

    # Lightroom commonly stores captureTime as "YYYY-MM-DD HH:MM:SS".
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y%m%d")
    except ValueError:
        pass

    # Fallback for values with fractional seconds/timezone or ISO-like variants.
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y%m%d")
    except ValueError:
        return None


def determine_identifier(
    creation_date: str,
    id_prefix: str,
    uniqifier_length: int = 12,
) -> str:
    """Build identifier as <prefix>_<YYYYMMDD>_<random-base62-uniqifier>."""
    if uniqifier_length < 1:
        raise ValueError("uniqifier_length must be >= 1")

    # Validate date format.
    datetime.strptime(creation_date, "%Y%m%d")

    uniqifier = "".join(secrets.choice(BASE62_ALPHABET) for _ in range(uniqifier_length))
    return f"{id_prefix}_{creation_date}_{uniqifier}"


def classify_metadata_target(file_path: str) -> str:
    """
    Classify how metadata should be written for a file path.

    Returns one of:
      - "skip_video"
      - "xmp_sidecar"
      - "in_file"
      - "unknown"
    """
    ext = Path(file_path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "skip_video"
    if ext in SIDECAR_METADATA_EXTENSIONS:
        return "xmp_sidecar"
    if ext in IN_FILE_METADATA_EXTENSIONS:
        return "in_file"
    return "unknown"


def metadata_target_path(file_path: str, metadata_location: str) -> str:
    """Return the path to read/write metadata for a given file and metadata location."""
    if metadata_location == "xmp_sidecar":
        return str(Path(file_path).with_suffix(".xmp"))
    return file_path


def get_dc_identifier(file_path: str, metadata_location: str) -> str | None:
    """Read dc:Identifier via exiftool from the selected metadata target path."""
    target_path = metadata_target_path(file_path, metadata_location)

    try:
        proc = subprocess.run(
            ["exiftool", "-j", "-XMP-dc:Identifier", target_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None

    if proc.returncode != 0:
        return None

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, list) or not payload:
        return None

    first = payload[0]
    if not isinstance(first, dict):
        return None

    value = first.get("Identifier")
    if value is None:
        value = first.get("XMP-dc:Identifier")

    if isinstance(value, str) and value.strip():
        return value.strip()

    return None


def set_dc_identifier(file_path: str, metadata_location: str, identifier: str) -> bool:
    """Write dc:Identifier via exiftool to the selected metadata target path."""
    target_path = metadata_target_path(file_path, metadata_location)
    try:
        proc = subprocess.run(
            ["exiftool", "-overwrite_original", f"-XMP-dc:Identifier={identifier}", target_path],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False

    return proc.returncode == 0


def resolve_manifest_row_identifier(
    row: dict[str, str],
    id_prefix: str,
) -> dict[str, str | bool]:
    """Resolve file checks + metadata target + found/generated identifier for one manifest row."""
    file_path = row["file_path"]
    creation_date = row["creation_date"]
    path_obj = Path(file_path)

    exists = path_obj.exists()
    metadata_target = classify_metadata_target(file_path)

    found_identifier = None
    if exists and metadata_target != "skip_video":
        found_identifier = get_dc_identifier(file_path, metadata_target)

    identifier = found_identifier or determine_identifier(
        creation_date=creation_date,
        id_prefix=id_prefix,
    )

    return {
        "file_path": file_path,
        "file_name": path_obj.name,
        "exists": exists,
        "metadata_location": metadata_target,
        "identifier": identifier,
        "identifier_source": "found" if found_identifier else "generated",
    }


def parse_cli_date(value: str) -> date:
    """Parse CLI date in YYYY-MM-DD format for range filters."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Expected format YYYY-MM-DD."
        ) from exc


def row_matches_filters(
    row: dict[str, str],
    extension_filter: str | None,
    from_date: date,
    to_date: date,
) -> bool:
    """AND-combined selection logic for extension/date range filters."""
    file_ext = Path(row["file_path"]).suffix.lower()
    if extension_filter and file_ext != extension_filter:
        return False

    row_date = datetime.strptime(row["creation_date"], "%Y%m%d").date()
    if row_date < from_date or row_date > to_date:
        return False

    return True


def _normalize_collection_name(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def resolve_collection_filter_ids(catalog_path: str, collection_name: str) -> list[int]:
    """
    Resolve a collection selector into regular collection IDs.

    If the selector names a regular collection, returns that one collection ID.
    If it names a collection set, returns all regular collections in its subtree.
    """
    query = """
        SELECT c.id_local, c.name, c.creationId, c.parent
        FROM AgLibraryCollection c
    """

    with sqlite3.connect(catalog_path) as conn:
        rows = conn.execute(query).fetchall()

    all_rows = [
        {
            "id_local": int(row[0]),
            "name": str(row[1]),
            "creation_id": str(row[2]) if row[2] is not None else "",
            "parent": int(row[3]) if row[3] is not None else None,
        }
        for row in rows
        if row and row[0] is not None and row[1] is not None
    ]

    def select_candidates(normalized: bool = False, case_insensitive: bool = False) -> list[dict[str, object]]:
        if normalized:
            target = _normalize_collection_name(collection_name)
            return [
                row
                for row in all_rows
                if _normalize_collection_name(str(row["name"])) == target
            ]
        if case_insensitive:
            target = collection_name.lower()
            return [row for row in all_rows if str(row["name"]).lower() == target]
        return [row for row in all_rows if str(row["name"]) == collection_name]

    candidates = select_candidates()
    if not candidates:
        candidates = select_candidates(case_insensitive=True)
    if not candidates:
        candidates = select_candidates(normalized=True)

    if len(candidates) > 1:
        candidate_names = sorted({str(c["name"]) for c in candidates})
        raise ValueError(
            f"Collection name '{collection_name}' is ambiguous. "
            f"Matches: {', '.join(candidate_names[:10])}"
        )

    if not candidates:
        norm_target = _normalize_collection_name(collection_name)
        suggestions = [
            str(row["name"])
            for row in all_rows
            if row["creation_id"] in {"com.adobe.ag.library.collection", "com.adobe.ag.library.group"}
            and (
                norm_target in _normalize_collection_name(str(row["name"]))
                or _normalize_collection_name(str(row["name"])) in norm_target
            )
        ]
        if suggestions:
            raise ValueError(
                f"Collection not found: '{collection_name}'. "
                f"Did you mean: {', '.join(sorted(set(suggestions))[:10])}?"
            )
        raise ValueError(
            f"Collection not found: '{collection_name}'. Use an existing regular collection "
            "or collection set name."
        )

    selected = candidates[0]
    selected_type = str(selected["creation_id"])

    if selected_type == "com.adobe.ag.library.smart_collection":
        raise ValueError(
            f"Collection '{collection_name}' is a smart collection. "
            "This script currently supports regular collections and collection sets only."
        )

    if selected_type == "com.adobe.ag.library.collection":
        return [int(str(selected["id_local"]))]

    if selected_type != "com.adobe.ag.library.group":
        raise ValueError(
            f"Collection '{collection_name}' exists but is not a supported collection type."
        )

    # Collection set: gather all descendant regular collections.
    children_by_parent: dict[int | None, list[dict[str, object]]] = {}
    for row in all_rows:
        children_by_parent.setdefault(row["parent"], []).append(row)

    root_id = int(str(selected["id_local"]))
    stack = [root_id]
    regular_ids: list[int] = []
    while stack:
        parent_id = stack.pop()
        for child in children_by_parent.get(parent_id, []):
            child_type = str(child["creation_id"])
            child_id = int(str(child["id_local"]))
            if child_type == "com.adobe.ag.library.collection":
                regular_ids.append(child_id)
            elif child_type == "com.adobe.ag.library.group":
                stack.append(child_id)

    if not regular_ids:
        raise ValueError(
            f"Collection set '{collection_name}' contains no regular collections in its subtree."
        )

    return sorted(set(regular_ids))


def build_exiftool_manifest(
    catalog_path: str,
    collection_ids: list[int] | None = None,
) -> list[dict[str, str]]:
    """
    Query a Lightroom catalog and build a manifest suitable for driving metadata writes.

    Each manifest row contains:
      - file_path: Full path to the image on disk.
      - creation_date: YYYYMMDD derived from Lightroom captureTime.

    Only folders whose path begins with "My Photos" are included.
    Rows without a parseable creation date are skipped.

    Args:
        catalog_path: Full path to the .lrcat file.

    Returns:
        A list of dictionaries with keys: "file_path" and "creation_date".

    Raises:
        FileNotFoundError: If catalog_path does not exist.
        ValueError: If catalog_path is not a .lrcat file.
    """
    src = Path(catalog_path).expanduser().resolve()

    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Catalog not found: {src}")
    if src.suffix.lower() != ".lrcat":
        raise ValueError(f"Expected a .lrcat file, got: {src.name}")

    query = """
        SELECT
            rf.absolutePath,
            fo.pathFromRoot,
            f.baseName,
            f.extension,
            i.captureTime
        FROM Adobe_images i
        JOIN AgLibraryFile f ON i.rootFile = f.id_local
        JOIN AgLibraryFolder fo ON f.folder = fo.id_local
        JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
    """

    where_clauses = [
        "fo.pathFromRoot LIKE 'My Photos%'",
        "i.captureTime IS NOT NULL",
        "i.captureTime != ''",
    ]
    params: list[str] = []

    if collection_ids:
        placeholders = ",".join("?" for _ in collection_ids)
        query += "\n        JOIN AgLibraryCollectionImage ci ON ci.image = i.id_local"
        where_clauses.append(f"ci.collection IN ({placeholders})")
        params.extend(str(c_id) for c_id in collection_ids)

    query += "\n        WHERE " + "\n          AND ".join(where_clauses)
    query += "\n        ORDER BY i.captureTime, f.id_local"

    results: list[dict[str, str]] = []
    with sqlite3.connect(str(src)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()

    for row in rows:
        absolute_path = row["absolutePath"] or ""
        path_from_root = row["pathFromRoot"] or ""
        base_name = row["baseName"] or ""
        extension = row["extension"] or ""
        capture_time = row["captureTime"]

        creation_date = parse_creation_date(capture_time)
        if not creation_date:
            continue

        root_folder = Path(absolute_path.replace("file://", "", 1))
        folder = root_folder / path_from_root
        filename = f"{base_name}.{extension}" if extension else base_name
        file_path = str((folder / filename).resolve())

        results.append(
            {
                "file_path": file_path,
                "creation_date": creation_date,
            }
        )

    return results




def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Process Lightroom catalog items in dry-run or run mode.\n"
            "\n"
            "If --catalog is omitted, the newest .lrcat in ~/Pictures/Lightroom is used.\n"
            "The catalog file is copied to a temporary system directory for safe reads.\n"
            "Selection criteria are ANDed: collection-name, extension, from-date, to-date."
        ),
        epilog=(
            "Examples:\n"
            "  python3 add-identifiers.py dry-run --collection-name 'Portfolio' --extension .jpg --from-date 2020-01-01 --to-date 2020-12-31 --limit 25\n"
            "  python3 add-identifiers.py run --id-prefix dd --limit 20\n"
            "  python3 add-identifiers.py run --catalog /path/to/catalog.lrcat --limit 20\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "mode",
        choices=["dry-run", "run"],
        help=(
            "Execution mode: dry-run (simulate writes) or run "
            "(write generated identifiers)."
        ),
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help=(
            "Path to Lightroom catalog (.lrcat). If omitted, the newest catalog in "
            "~/Pictures/Lightroom is used."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help=(
            "Maximum number of rows to process after filtering (default: 50). "
            "In run and dry-run mode, only successful writes (or would-write events) "
            "count toward this limit."
        ),
    )
    parser.add_argument(
        "--id-prefix",
        default="dd",
        help=(
            "Prefix for generated identifiers when dc:Identifier is missing "
            "(default: dd)"
        ),
    )

    parser.add_argument(
        "--collection-name",
        default=None,
        help=(
            "Optional Lightroom collection selector. Can be a regular collection name "
            "or a collection set name (includes all regular collections in its subtree)."
        ),
    )
    parser.add_argument(
        "--extension",
        default=None,
        help=(
            "Optional file extension criterion (case-insensitive), e.g. '.cr3' or 'cr3'. "
            "If omitted, all extensions are included."
        ),
    )
    parser.add_argument(
        "--from-date",
        type=parse_cli_date,
        default=date(1900, 1, 1),
        help=(
            "Inclusive start-date criterion in YYYY-MM-DD format "
            "(default: 1900-01-01)"
        ),
    )
    parser.add_argument(
        "--to-date",
        type=parse_cli_date,
        default=date(2099, 12, 31),
        help=(
            "Inclusive end-date criterion in YYYY-MM-DD format "
            "(default: 2099-12-31)"
        ),
    )

    args = parser.parse_args()

    if args.from_date > args.to_date:
        parser.error("--from-date must be on or before --to-date")

    try:
        catalog_path = resolve_catalog_path(args.catalog)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    extension_filter = None
    if args.extension:
        extension_filter = args.extension.lower()
        if not extension_filter.startswith("."):
            extension_filter = f".{extension_filter}"

    with tempfile.TemporaryDirectory(prefix="lr_catalog_test_") as tmp:
        copied_catalog = copy_lightroom_catalog_to_temp(catalog_path, temp_dir=tmp)
        copied_catalog_str = str(copied_catalog)

        resolved_collection_ids: list[int] | None = None
        if args.collection_name:
            try:
                resolved_collection_ids = resolve_collection_filter_ids(
                    copied_catalog_str,
                    args.collection_name,
                )
            except ValueError as exc:
                parser.error(str(exc))

        manifest = build_exiftool_manifest(
            copied_catalog_str,
            collection_ids=resolved_collection_ids,
        )

    filtered_manifest = [
        row
        for row in manifest
        if row_matches_filters(
            row,
            extension_filter=extension_filter,
            from_date=args.from_date,
            to_date=args.to_date,
        )
    ]

    limit = max(args.limit, 0)

    if shutil.which("exiftool") is None:
        parser.error("exiftool is required for dry-run/run mode and was not found in PATH")

    is_dry_run = args.mode == "dry-run"

    written_count = 0
    dry_run_would_write = 0
    bypassed_existing = 0
    bypassed_nonprocessable = 0
    write_failures = 0

    for row in filtered_manifest:
        if written_count >= limit:
            break

        resolved = resolve_manifest_row_identifier(row, id_prefix=args.id_prefix)

        if not resolved["exists"] or resolved["metadata_location"] == "skip_video":
            bypassed_nonprocessable += 1
            continue

        if resolved["identifier_source"] == "found":
            bypassed_existing += 1
            print(
                f"BYPASS existing id: "
                f"file={resolved['file_name']} | "
                f"metadata_location={resolved['metadata_location']} | "
                f"identifier={resolved['identifier']}"
            )
            continue

        if is_dry_run:
            dry_run_would_write += 1
            written_count += 1
            print(
                f"DRY-RUN WOULD WRITE: "
                f"file={resolved['file_name']} | "
                f"metadata_location={resolved['metadata_location']} | "
                f"identifier={resolved['identifier']}"
            )
            continue

        was_written = set_dc_identifier(
            file_path=str(resolved["file_path"]),
            metadata_location=str(resolved["metadata_location"]),
            identifier=str(resolved["identifier"]),
        )

        if was_written:
            written_count += 1
            print(
                f"WROTE: "
                f"file={resolved['file_name']} | "
                f"metadata_location={resolved['metadata_location']} | "
                f"identifier={resolved['identifier']}"
            )
        else:
            write_failures += 1
            print(
                f"WRITE FAILED: "
                f"file={resolved['file_name']} | "
                f"metadata_location={resolved['metadata_location']} | "
                f"identifier={resolved['identifier']}"
            )

    print(
        f"\nProcessing summary: mode={'dry-run' if is_dry_run else 'write'}, "
        f"wrote={written_count}, "
        f"dry_run_would_write={dry_run_would_write}, "
        f"bypassed_existing={bypassed_existing}, "
        f"bypassed_nonprocessable={bypassed_nonprocessable}, "
        f"write_failures={write_failures}, "
        f"filtered_total={len(filtered_manifest)}, "
        f"limit={limit}"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
