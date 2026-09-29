#!/bin/bash
set -euo pipefail

WATCH_FOLDER="${CWA_METADATA_CHANGE_LOGS_DIR:-/opt/calibre-web-nextgen/config/metadata_change_logs}"
mkdir -p "$WATCH_FOLDER"

echo "[metadata-change-detector] Watching folder: $WATCH_FOLDER"

if command -v inotifywait >/dev/null 2>&1; then
    echo "[metadata-change-detector] Using inotifywait watcher"
    inotifywait -m -e close_write -e moved_to --format '%f' --exclude '.*\.swp$' "$WATCH_FOLDER" \
      | python3 /opt/calibre-web-nextgen/scripts/metadata_change_dispatch.py --watch-folder "$WATCH_FOLDER"
else
    echo "[metadata-change-detector] inotifywait not available; using polling fallback"
    python3 /opt/calibre-web-nextgen/scripts/watch_fallback.py \
      --path "$WATCH_FOLDER" \
      --interval 5 \
      --exts "json,log" \
      | while read -r events filepath; do
          basename -- "$filepath"
        done \
      | python3 /opt/calibre-web-nextgen/scripts/metadata_change_dispatch.py --watch-folder "$WATCH_FOLDER"
fi
