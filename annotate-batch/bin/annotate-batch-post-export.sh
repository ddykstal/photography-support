#!/usr/bin/env bash
set -u

# Lightroom Classic post-export helper for annotate-batch.
#
# Usage:
#   annotate-batch-post-export.sh <export_dir> [output_dir]
#
# Notes:
# - <export_dir> should be the folder LrC exported images into.
# - [output_dir] defaults to <export_dir>/annotated.
# - Set ANNOTATE_PROFILE to override the default profile.

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: $(basename "$0") <export_dir> [output_dir]" >&2
  exit 2
fi

EXPORT_DIR="$1"
OUTPUT_DIR="${2:-$EXPORT_DIR/annotated}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${ANNOTATE_PYTHON_BIN:-$APP_ROOT/.venv/bin/python}"
ANNOTATE_SCRIPT="$APP_ROOT/bin/annotate-batch.py"

if [ ! -d "$EXPORT_DIR" ]; then
  echo "Error: export directory does not exist: $EXPORT_DIR" >&2
  exit 1
fi

if [ ! -x "$PYTHON_BIN" ]; then
  echo "Error: python interpreter not found/executable: $PYTHON_BIN" >&2
  echo "Expected virtualenv at: $APP_ROOT/.venv" >&2
  exit 1
fi

if [ ! -f "$ANNOTATE_SCRIPT" ]; then
  echo "Error: annotate script not found: $ANNOTATE_SCRIPT" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

CMD=(
  "$PYTHON_BIN" "$ANNOTATE_SCRIPT"
  --upload-dir "$EXPORT_DIR"
  --download-dir "$OUTPUT_DIR"
)

if [ -n "${ANNOTATE_PROFILE:-}" ]; then
  CMD+=(--profile "$ANNOTATE_PROFILE")
fi

echo "Running annotate-batch..."
echo "  upload-dir:   $EXPORT_DIR"
echo "  download-dir: $OUTPUT_DIR"
if [ -n "${ANNOTATE_PROFILE:-}" ]; then
  echo "  profile:      $ANNOTATE_PROFILE"
fi

"${CMD[@]}"
status=$?

# Always open Finder so you can review what was generated.
open "$OUTPUT_DIR" >/dev/null 2>&1 || true

if [ "$status" -ne 0 ]; then
  echo "annotate-batch finished with errors (exit $status)." >&2
else
  echo "annotate-batch completed successfully."
fi

echo "Opened Finder at: $OUTPUT_DIR"
exit "$status"
