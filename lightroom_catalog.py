from pathlib import Path
import shutil
import tempfile


DEFAULT_LIGHTROOM_DIR = Path("~/Pictures/Lightroom").expanduser()


def find_latest_lightroom_catalog(lightroom_dir: Path | None = None) -> Path | None:
    """Return the most recently modified .lrcat in lightroom_dir, or None if none exist."""
    search_dir = lightroom_dir if lightroom_dir is not None else DEFAULT_LIGHTROOM_DIR
    catalogs = [p for p in search_dir.glob("*.lrcat") if p.is_file()]
    if not catalogs:
        return None
    return max(catalogs, key=lambda p: p.stat().st_mtime)


def resolve_catalog_path(
    catalog: str | Path | None,
    lightroom_dir: Path | None = None,
) -> Path:
    """
    Resolve an explicit catalog path or auto-select the newest catalog from lightroom_dir.

    Raises FileNotFoundError if no suitable catalog can be resolved.
    Raises ValueError if a resolved path is not a .lrcat file.
    """
    if catalog:
        resolved = Path(catalog).expanduser().resolve()
    else:
        latest = find_latest_lightroom_catalog(lightroom_dir)
        if latest is None:
            search_dir = lightroom_dir if lightroom_dir is not None else DEFAULT_LIGHTROOM_DIR
            raise FileNotFoundError(f"No Lightroom catalog found in {search_dir}")
        resolved = latest

    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"Catalog not found: {resolved}")
    if resolved.suffix.lower() != ".lrcat":
        raise ValueError(f"Expected a .lrcat file, got: {resolved.name}")

    return resolved


def copy_lightroom_catalog_to_temp(
    catalog_path: str | Path,
    temp_dir: str | Path | None = None,
    prefix: str = "lr_catalog_",
) -> Path:
    """Copy a Lightroom catalog to a temporary directory and return the copied path."""
    src = resolve_catalog_path(catalog_path)

    if temp_dir is None:
        temp_root = Path(tempfile.mkdtemp(prefix=prefix))
    else:
        temp_root = Path(temp_dir).expanduser().resolve()
    temp_root.mkdir(parents=True, exist_ok=True)

    dst = temp_root / src.name
    shutil.copy2(src, dst)
    return dst
