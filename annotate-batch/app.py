#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename


APP_DIR = Path(__file__).resolve().parent
BIN_DIR = APP_DIR / "bin"
UPLOAD_DIR = APP_DIR / "uploads"
ANNOTATED_DIR = APP_DIR / "annotated"
ANNOTATE_SCRIPT = BIN_DIR / "annotate-border.py"
DEFAULT_PROFILE = BIN_DIR / "profiles" / "annotation-screen-footer.annotate"
ALLOWED_EXTENSIONS = {".jpg", ".jpeg"}


def load_annotator_module(script_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("annotate_border", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {script_path}")
    module = importlib.util.module_from_spec(spec)
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


def list_annotated_files() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(ANNOTATED_DIR.glob("*.jpg")):
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


def create_app() -> Flask:
    app = Flask(__name__)

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)

    annotator = load_annotator_module(ANNOTATE_SCRIPT)

    @app.get("/")
    def index() -> str:
        return render_template("index.html", files=list_annotated_files())

    @app.get("/api/files")
    def api_files() -> Any:
        return jsonify({"files": list_annotated_files()})

    @app.post("/upload")
    def upload() -> Any:
        profile_path = current_profile_path()
        if not profile_path.exists():
            return jsonify({"error": f"Profile not found: {profile_path}"}), 500

        incoming = request.files.getlist("files")
        if not incoming:
            return jsonify({"error": "No files uploaded (use field name 'files')."}), 400

        try:
            profile = annotator.load_profile(profile_path)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": f"Could not load profile: {exc}"}), 500

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
            upload_path = UPLOAD_DIR / upload_name
            output_name = f"{Path(upload_name).stem}.annotated.jpg"
            output_path = ANNOTATED_DIR / output_name

            try:
                file_storage.save(upload_path)
                annotator.annotate_image(upload_path, output_path, profile)
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
                "files": list_annotated_files(),
            }
        )

    @app.get("/files/<path:filename>")
    def view_file(filename: str) -> Any:
        return send_from_directory(ANNOTATED_DIR, filename, as_attachment=False)

    @app.get("/download/<path:filename>")
    def download_file(filename: str) -> Any:
        return send_from_directory(ANNOTATED_DIR, filename, as_attachment=True)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True)
