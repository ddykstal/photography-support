#!/usr/bin/env zsh
#
# Preflight wrapper for Lightroom backup routines.
#
# Behavior:
# 1) Run lock check with defaults.
# 2) If lock check succeeds, run backup retention with defaults.
# 3) If lock check fails, exit 1.
#

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK_CHECK_SCRIPT="$SCRIPT_DIR/is-lightroom-locked.zsh"
RETENTION_SCRIPT="$SCRIPT_DIR/lrc-backup-retention.py"

if ! "$LOCK_CHECK_SCRIPT"; then
  exit 1
fi

python3 "$RETENTION_SCRIPT"
exit $?
