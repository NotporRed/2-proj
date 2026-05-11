#!/bin/sh
set -e
if [ -n "${GTFS_ZIP_PATH}" ] && [ -f "${GTFS_ZIP_PATH}" ]; then
  python -c "from gtfs_loader import ensure_loaded; ensure_loaded()"
else
  python -c "from netex_loader import ensure_dataset; ensure_dataset()"
fi
exec gunicorn --timeout 900 -w 1 -b 0.0.0.0:5001 app:app
