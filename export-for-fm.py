import argparse
import csv
import json
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from lightroom_catalog import copy_lightroom_catalog_to_temp, resolve_catalog_path

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

EXPORT_TAGS = [
    "XMP-dc:Identifier",
    "XMP-dc:Title",
    "XMP-dc:Description",
    "XMP-dc:Rights",
    "XMP-xmpRights:UsageTerms",
    "EXIF:DateTimeOriginal",
    "XMP-xmp:CreateDate",
    "EXIF:Make",
    "EXIF:Model",
    "EXIF:ISO",
    "EXIF:FNumber",
    "EXIF:ExposureTime",
    "EXIF:LensModel",
]


def run_exiftool_json(file_path: Path, tags: list[str]) -> dict[str, object]:
    proc = shutil.which("exiftool")
    if proc is None:
        raise RuntimeError("exiftool not found in PATH")

    import subprocess

    result = subprocess.run(
        [proc, "-j", *[f"-{t}" for t in tags], str(file_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"exiftool read failed for {file_path}: {result.stderr.strip()}")

    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        return {}
    return payload[0]


def get_tag(payload: dict[str, object], tag: str) -> str:
    short_key = tag.split(":", 1)[1]
    value = payload.get(short_key)
    if value is None:
        value = payload.get(tag)
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def parse_capture_date(value: str) -> str:
    if not value:
        return ""
    candidates = [
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in candidates:
        try:
            return datetime.strptime(value[:19], fmt).replace(tzinfo=timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def normalize_id_global(value: str) -> str:
    normalized = value.strip()
    if normalized.lower().startswith("uuid:"):
        normalized = normalized[5:]
    return normalized.strip("{}")


def metadata_target_path_for_source(file_path: Path) -> Path:
    if file_path.suffix.lower() in SIDECAR_METADATA_EXTENSIONS:
        return file_path.with_suffix(".xmp")
    return file_path


def merge_exiftool_payloads(primary: dict[str, object], secondary: dict[str, object]) -> dict[str, object]:
    """Merge secondary over primary, but only with non-empty values."""
    merged = dict(primary)
    for key, value in secondary.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not any(str(v).strip() for v in value):
            continue
        merged[key] = value
    return merged


def read_combined_source_metadata(source_file_path: Path, tags: list[str]) -> dict[str, object]:
    """Read metadata from source file and merge sidecar metadata (if present)."""
    payload_from_file = run_exiftool_json(source_file_path, tags)

    sidecar_path = source_file_path.with_suffix(".xmp")
    if source_file_path.suffix.lower() in SIDECAR_METADATA_EXTENSIONS and sidecar_path.exists():
        payload_from_sidecar = run_exiftool_json(sidecar_path, tags)
        return merge_exiftool_payloads(payload_from_file, payload_from_sidecar)

    return payload_from_file


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


def build_catalog_manifest(catalog_path: str, collection_ids: list[int]) -> list[dict[str, str]]:
    src = Path(catalog_path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"Catalog not found: {src}")
    if src.suffix.lower() != ".lrcat":
        raise ValueError(f"Expected a .lrcat file, got: {src.name}")

    placeholders = ",".join("?" for _ in collection_ids)
    query = f"""
        SELECT
            rf.absolutePath,
            fo.pathFromRoot,
            f.baseName,
            f.extension,
            i.id_global AS idGlobal,
            i.captureTime
        FROM Adobe_images i
        JOIN AgLibraryCollectionImage ci ON ci.image = i.id_local
        JOIN AgLibraryFile f ON i.rootFile = f.id_local
        JOIN AgLibraryFolder fo ON f.folder = fo.id_local
        JOIN AgLibraryRootFolder rf ON fo.rootFolder = rf.id_local
        WHERE fo.pathFromRoot LIKE 'My Photos%'
          AND ci.collection IN ({placeholders})
        ORDER BY i.captureTime, f.id_local
    """

    with sqlite3.connect(str(src)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, [str(c_id) for c_id in collection_ids]).fetchall()

    result: list[dict[str, str]] = []
    for row in rows:
        root = Path((row["absolutePath"] or "").replace("file://", "", 1))
        path_from_root = row["pathFromRoot"] or ""
        base_name = row["baseName"] or ""
        extension = row["extension"] or ""
        filename = f"{base_name}.{extension}" if extension else base_name
        source_path = (root / path_from_root / filename).resolve()

        result.append(
            {
                "source_path": str(source_path.parent),
                "source_filename": filename,
                "source_full_path": str(source_path),
                "catalog_id_global": normalize_id_global(str(row["idGlobal"] or "")),
                "catalog_capture_date": parse_capture_date(str(row["captureTime"] or "")),
            }
        )

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export metadata rows for FileMaker import from Lightroom catalog collection "
            "members, reading metadata from source files/sidecars."
        ),
        epilog=(
            "Examples:\n"
            "  python3 export-for-fm.py --collection-name '#rpc_shadows'\n"
            "  python3 export-for-fm.py --collection-name 'Portfolio Set' --format csv --output fm-images.csv\n"
            "  python3 export-for-fm.py --collection-name '#rpc_shadows' --catalog /path/to/catalog.lrcat\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--collection-name",
        required=True,
        help=(
            "Lightroom collection selector. Can be a regular collection name or a "
            "collection set name (includes all regular collections in its subtree)."
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
        "--format",
        choices=["tsv", "csv"],
        default="tsv",
        help="Output format (default: tsv).",
    )
    parser.add_argument(
        "--output",
        default="fm-images.tsv",
        help="Output file path (default: fm-images.tsv).",
    )

    args = parser.parse_args()

    if shutil.which("exiftool") is None:
        parser.error("exiftool is required and was not found in PATH")

    try:
        catalog_path = resolve_catalog_path(args.catalog)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))

    output_path = Path(args.output).expanduser().resolve()
    delimiter = "\t" if args.format == "tsv" else ","

    fields = [
        "dc_identifier",
        "catalog_id_global",
        "source_path",
        "source_filename",
        "export_path",
        "export_filename",
        "capture_date",
        "title",
        "caption",
        "copyright",
        "license",
        "camera_make",
        "camera_model",
        "iso",
        "aperture",
        "shutter_speed",
        "lens_model",
        "status",
        "reason",
    ]

    rows_out: list[dict[str, str]] = []
    seen_identifiers: set[str] = set()

    with tempfile.TemporaryDirectory(prefix="fm_catalog_") as tmp:
        catalog_copy = copy_lightroom_catalog_to_temp(catalog_path, tmp)
        catalog_copy_str = str(catalog_copy)

        try:
            collection_ids = resolve_collection_filter_ids(catalog_copy_str, args.collection_name)
        except ValueError as exc:
            parser.error(str(exc))

        manifest = build_catalog_manifest(catalog_copy_str, collection_ids)
        if not manifest:
            print("No images found for selected collection(s).")
            return 0

        for item in manifest:
            source_full_path = Path(item["source_full_path"])
            source_meta_target = metadata_target_path_for_source(source_full_path)

            row = {
                "dc_identifier": "",
                "catalog_id_global": item["catalog_id_global"],
                "source_path": item["source_path"],
                "source_filename": item["source_filename"],
                # Kept for schema compatibility with prior export-based version.
                "export_path": item["source_path"],
                "export_filename": item["source_filename"],
                "capture_date": item["catalog_capture_date"],
                "title": "",
                "caption": "",
                "copyright": "",
                "license": "",
                "camera_make": "",
                "camera_model": "",
                "iso": "",
                "aperture": "",
                "shutter_speed": "",
                "lens_model": "",
                "status": "",
                "reason": "",
            }

            if not source_meta_target.exists():
                row["status"] = "failed"
                row["reason"] = f"source_metadata_target_missing: {source_meta_target}"
                rows_out.append(row)
                continue

            try:
                payload = read_combined_source_metadata(source_full_path, EXPORT_TAGS)
            except (RuntimeError, ValueError) as exc:
                row["status"] = "failed"
                row["reason"] = f"metadata_read_failed: {exc}"
                rows_out.append(row)
                continue

            dc_identifier = get_tag(payload, "XMP-dc:Identifier")
            row["dc_identifier"] = dc_identifier
            row["title"] = get_tag(payload, "XMP-dc:Title")
            row["caption"] = get_tag(payload, "XMP-dc:Description")
            row["copyright"] = get_tag(payload, "XMP-dc:Rights")
            row["license"] = get_tag(payload, "XMP-xmpRights:UsageTerms")
            row["camera_make"] = get_tag(payload, "EXIF:Make")
            row["camera_model"] = get_tag(payload, "EXIF:Model")
            row["iso"] = get_tag(payload, "EXIF:ISO")
            row["aperture"] = get_tag(payload, "EXIF:FNumber")
            row["shutter_speed"] = get_tag(payload, "EXIF:ExposureTime")
            row["lens_model"] = get_tag(payload, "EXIF:LensModel")

            metadata_date = parse_capture_date(
                get_tag(payload, "EXIF:DateTimeOriginal") or get_tag(payload, "XMP-xmp:CreateDate")
            )
            if metadata_date:
                row["capture_date"] = metadata_date

            if not dc_identifier:
                row["status"] = "skipped"
                row["reason"] = "missing_dc_identifier"
                rows_out.append(row)
                continue

            if dc_identifier in seen_identifiers:
                row["status"] = "skipped"
                row["reason"] = "duplicate_dc_identifier_in_collection"
                rows_out.append(row)
                continue

            seen_identifiers.add(dc_identifier)
            row["status"] = "ok"
            row["reason"] = ""
            rows_out.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(rows_out)

    ok_count = sum(1 for r in rows_out if r["status"] == "ok")
    skip_count = sum(1 for r in rows_out if r["status"] == "skipped")
    fail_count = sum(1 for r in rows_out if r["status"] == "failed")

    print(f"Wrote {len(rows_out)} rows to {output_path}")
    print(f"Summary: ok={ok_count}, skipped={skip_count}, failed={fail_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
