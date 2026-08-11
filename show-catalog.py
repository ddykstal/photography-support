import argparse
import json
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path

from lightroom_catalog import copy_lightroom_catalog_to_temp, resolve_catalog_path

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
    if not capture_time:
        return None

    raw = str(capture_time)
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y%m%d")
    except ValueError:
        pass

    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y%m%d")
    except ValueError:
        return None


def classify_metadata_target(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "skip_video"
    if ext in SIDECAR_METADATA_EXTENSIONS:
        return "xmp_sidecar"
    if ext in IN_FILE_METADATA_EXTENSIONS:
        return "in_file"
    return "unknown"


def metadata_target_path(file_path: str, metadata_location: str) -> str:
    if metadata_location == "xmp_sidecar":
        return str(Path(file_path).with_suffix(".xmp"))
    return file_path


def get_dc_identifier(file_path: str, metadata_location: str) -> str | None:
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


def parse_cli_date(value: str) -> date:
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
    file_ext = Path(row["file_path"]).suffix.lower()
    if extension_filter and file_ext != extension_filter:
        return False

    row_date = datetime.strptime(row["creation_date"], "%Y%m%d").date()
    return from_date <= row_date <= to_date


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
        creation_date = parse_creation_date(row["captureTime"])
        if not creation_date:
            continue

        root_folder = Path((row["absolutePath"] or "").replace("file://", "", 1))
        path_from_root = row["pathFromRoot"] or ""
        base_name = row["baseName"] or ""
        extension = row["extension"] or ""

        filename = f"{base_name}.{extension}" if extension else base_name
        file_path = str((root_folder / path_from_root / filename).resolve())

        results.append({"file_path": file_path, "creation_date": creation_date})

    return results




def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|")


def sidecar_status_for(file_path: str, metadata_location: str) -> tuple[str, str]:
    if metadata_location != "xmp_sidecar":
        return ("not-required", "")

    sidecar_path = str(Path(file_path).with_suffix(".xmp"))
    if Path(sidecar_path).exists():
        return ("required-present", sidecar_path)
    return ("required-missing", sidecar_path)


def build_list_markdown(rows: list[dict[str, str]], title: str) -> str:
    lines = [
        f"# {title}",
        "",
        "| Location | File Name | Sidecar Status | Identifier |",
        "| --- | --- | --- | --- |",
    ]

    for row in rows:
        file_path = row["file_path"]
        file_obj = Path(file_path)
        location = str(file_obj.parent)
        metadata_location = classify_metadata_target(file_path)
        sidecar_status, _ = sidecar_status_for(file_path, metadata_location)

        identifier = ""
        if file_obj.exists() and metadata_location != "skip_video":
            identifier = get_dc_identifier(file_path, metadata_location) or ""

        lines.append(
            "| "
            f"{markdown_escape(location)} | "
            f"{markdown_escape(file_obj.name)} | "
            f"{markdown_escape(sidecar_status)} | "
            f"{markdown_escape(identifier)} |"
        )

    lines.append("")
    lines.append(f"Rows listed: {len(rows)}")
    lines.append("")
    return "\n".join(lines)


def build_summary_markdown(rows: list[dict[str, str]], title: str) -> str:
    summary: dict[str, dict[str, int]] = {}

    for row in rows:
        file_path = row["file_path"]
        file_ext = Path(file_path).suffix.lower() or "[no extension]"
        bucket = summary.setdefault(
            file_ext,
            {"total": 0, "already_has_id": 0, "requires_id": 0},
        )
        bucket["total"] += 1

        exists = Path(file_path).exists()
        metadata_location = classify_metadata_target(file_path)
        processable = exists and metadata_location != "skip_video"
        if not processable:
            continue

        identifier = get_dc_identifier(file_path, metadata_location)
        if identifier:
            bucket["already_has_id"] += 1
        else:
            bucket["requires_id"] += 1

    lines = [
        f"# {title}",
        "",
        "| File Type | Total | Already Has ID | Requires ID |",
        "| --- | ---: | ---: | ---: |",
    ]

    grand_total = 0
    grand_has = 0
    grand_requires = 0

    for file_type in sorted(summary.keys()):
        row_sum = summary[file_type]
        grand_total += row_sum["total"]
        grand_has += row_sum["already_has_id"]
        grand_requires += row_sum["requires_id"]
        lines.append(
            f"| {markdown_escape(file_type)} | {row_sum['total']} | "
            f"{row_sum['already_has_id']} | {row_sum['requires_id']} |"
        )

    lines.append(
        f"| **TOTAL** | **{grand_total}** | **{grand_has}** | **{grand_requires}** |"
    )
    lines.append("")
    lines.append(f"Rows summarized: {len(rows)}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Show Lightroom catalog references as markdown list or summary.\n"
            "\n"
            "If --catalog is omitted, the newest .lrcat in ~/Pictures/Lightroom is used.\n"
            "The catalog file is copied to a temporary system directory for safe reads.\n"
            "Selection criteria are ANDed: collection-name, extension, from-date, to-date."
        ),
        epilog=(
            "Examples:\n"
            "  python3 show-catalog.py list --limit 50\n"
            "  python3 show-catalog.py summary --collection-name 'Portfolio' --extension cr3 --limit 200\n"
            "  python3 show-catalog.py list --from-date 2020-01-01 --to-date 2020-12-31\n"
            "  python3 show-catalog.py list --catalog /path/to/catalog.lrcat --limit 50\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "mode",
        choices=["list", "summary"],
        help="Output mode: list rows or summarize by file type.",
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
        help="Maximum number of filtered rows to examine (default: 50).",
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
        help="Optional file extension criterion (case-insensitive), e.g. '.cr3' or 'cr3'.",
    )
    parser.add_argument(
        "--from-date",
        type=parse_cli_date,
        default=date(1900, 1, 1),
        help="Inclusive start-date criterion in YYYY-MM-DD format (default: 1900-01-01).",
    )
    parser.add_argument(
        "--to-date",
        type=parse_cli_date,
        default=date(2099, 12, 31),
        help="Inclusive end-date criterion in YYYY-MM-DD format (default: 2099-12-31).",
    )
    parser.add_argument(
        "--output",
        default="show-catalog.md",
        help="Markdown output file path (default: show-catalog.md).",
    )

    args = parser.parse_args()

    if args.from_date > args.to_date:
        parser.error("--from-date must be on or before --to-date")
    if shutil.which("exiftool") is None:
        parser.error("exiftool is required and was not found in PATH")

    try:
        catalog_path = resolve_catalog_path(args.catalog)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    extension_filter = None
    if args.extension:
        extension_filter = args.extension.lower()
        if not extension_filter.startswith("."):
            extension_filter = f".{extension_filter}"

    with tempfile.TemporaryDirectory(prefix="lr_catalog_show_") as tmp:
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

    rows = filtered_manifest[: max(args.limit, 0)]

    if args.mode == "list":
        markdown = build_list_markdown(rows, "Catalog File List")
    else:
        markdown = build_summary_markdown(rows, "Catalog File Summary")

    output_path = Path(args.output).expanduser().resolve()
    output_path.write_text(markdown, encoding="utf-8")

    print(f"Wrote markdown output: {output_path}")

    open_proc = subprocess.run(
        ["open", "-a", "Marked", str(output_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if open_proc.returncode == 0:
        print("Opened output in Marked.")
    else:
        print("Could not open output in Marked. Is Marked installed?")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
