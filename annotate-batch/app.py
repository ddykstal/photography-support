#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_from_directory, session
from werkzeug.utils import secure_filename


APP_DIR = Path(__file__).resolve().parent
ANNOTATE_DIR = APP_DIR.parent / "annotate-border"
UPLOAD_DIR = APP_DIR / "uploads"
ANNOTATED_DIR = APP_DIR / "annotated"
ANNOTATE_SCRIPT = ANNOTATE_DIR / "annotate-border-v2.py"
DEFAULT_PROFILE = ANNOTATE_DIR / "profiles" / "annotation-v2-demo.annotate"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg"}
SESSION_COOKIE_KEY = "annotate_batch_session_id"
DEFAULT_SESSION_SECRET = "annotate-batch-dev-secret-change-me"


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


def current_profile_path() -> Path:
    configured = os.environ.get("ANNOTATE_PROFILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_PROFILE


def is_allowed_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def uniquify_filename(base_name: str) -> str:
    stem = Path(base_name).stem
    ext = Path(base_name).suffix.lower() or ".jpg"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    token = secrets.token_hex(3)
    safe_stem = secure_filename(stem) or "image"
    return f"{safe_stem}-{stamp}-{token}{ext}"


def current_session_id() -> str:
    session_id = session.get(SESSION_COOKIE_KEY, "")
    if isinstance(session_id, str) and session_id:
        return session_id

    new_session_id = secrets.token_hex(16)
    session[SESSION_COOKIE_KEY] = new_session_id
    return new_session_id


def session_upload_dir() -> Path:
    path = UPLOAD_DIR / current_session_id()
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_annotated_dir() -> Path:
    path = ANNOTATED_DIR / current_session_id()
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_annotated_files(directory: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jpg")):
        stat = path.stat()
        items.append(
            {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
    return items


def clear_directory_children(root: Path) -> int:
    removed = 0
    if not root.exists():
        return removed

    for child in root.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
            removed += 1
        else:
            child.unlink()
            removed += 1

    return removed


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("ANNOTATE_SESSION_SECRET", DEFAULT_SESSION_SECRET)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)

    annotator = load_annotator_module(ANNOTATE_SCRIPT)

    @app.get("/")
    def index() -> str:
        return render_template("index.html", files=list_annotated_files(session_annotated_dir()))

    @app.get("/api/files")
    def api_files() -> Any:
        return jsonify({"files": list_annotated_files(session_annotated_dir())})

    @app.post("/upload")
    def upload() -> Any:
        profile_path = current_profile_path()
        if not profile_path.exists():
            return jsonify({"error": f"Profile not found: {profile_path}"}), 500

        incoming = request.files.getlist("files")
        if not incoming:
            return jsonify({"error": "No files uploaded (use field name 'files')."}), 400

        try:
            profile = annotator.parse_profile_v2(profile_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not load V2 profile: {exc}"}), 500

        upload_dir = session_upload_dir()
        annotated_dir = session_annotated_dir()

        results: list[dict[str, str]] = []

        for file_storage in incoming:
            original_name = file_storage.filename or ""
            if not original_name:
                results.append({"file": "", "status": "skipped", "message": "missing filename"})
                continue
            if not is_allowed_image(original_name):
                results.append(
                    {
                        "file": original_name,
                        "status": "skipped",
                        "message": "unsupported extension (jpg/jpeg only)",
                    }
                )
                continue

            upload_name = uniquify_filename(original_name)
            upload_path = upload_dir / upload_name
            output_name = f"{Path(upload_name).stem}.annotated.jpg"
            output_path = annotated_dir / output_name

            try:
                file_storage.save(upload_path)
                annotator.annotate_v2(upload_path, output_path, profile, diagnostics=False)
                results.append(
                    {
                        "file": original_name,
                        "status": "ok",
                        "output": output_name,
                        "message": "annotated",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "file": original_name,
                        "status": "failed",
                        "message": str(exc),
                    }
                )

        return jsonify(
            {
                "profile": str(profile_path),
                "results": results,
                "files": list_annotated_files(annotated_dir),
            }
        )

    @app.post("/api/clear")
    def api_clear() -> Any:
        try:
            removed_upload_items = clear_directory_children(UPLOAD_DIR)
            removed_annotated_items = clear_directory_children(ANNOTATED_DIR)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Failed to clear storage: {exc}"}), 500

        # Start a fresh isolated bucket for the current browser after global wipe.
        session.pop(SESSION_COOKIE_KEY, None)

        return jsonify(
            {
                "status": "ok",
                "removed_upload_items": removed_upload_items,
                "removed_annotated_items": removed_annotated_items,
            }
        )

    @app.get("/files/<path:filename>")
    def view_file(filename: str) -> Any:
        annotated_dir = session_annotated_dir()
        if not (annotated_dir / filename).is_file():
            abort(404)
        return send_from_directory(annotated_dir, filename, as_attachment=False)

    @app.get("/download/<path:filename>")
    def download_file(filename: str) -> Any:
        annotated_dir = session_annotated_dir()
        if not (annotated_dir / filename).is_file():
            abort(404)
        return send_from_directory(annotated_dir, filename, as_attachment=True)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
