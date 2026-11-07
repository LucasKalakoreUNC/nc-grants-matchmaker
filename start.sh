#!/bin/sh
# Container entrypoint.
#   MODE=app (default): serve the site on $PORT; refresh data every $REFRESH_HOURS (default 24).
#   MODE=job: run the data refresh once and exit.
set -e
cd /app
mkdir -p data
# First start: unpack the committed database snapshot so the site has data immediately.
if [ ! -f data/matchmaker.db ] && [ -f data/seed/matchmaker.db.gz ]; then
  echo "Unpacking database snapshot..."
  gunzip -c data/seed/matchmaker.db.gz > data/matchmaker.db
fi
if [ "$MODE" = "job" ]; then
  exec python3 pipeline.py
fi
export REFRESH_HOURS="${REFRESH_HOURS:-24}"
exec python3 app.py
