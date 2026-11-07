#!/bin/sh
# Start the NC Grants Matchmaker.
#   MODE=app (default): serve the site on $PORT (8000); refresh data every
#                       $REFRESH_HOURS hours (default 24; 0 turns it off).
#   MODE=job:           run the data refresh once and exit.
#
# Works in the Docker image (/app) and on Azure App Service (Python), where
# the code folder is replaced on every deploy: there the database lives in
# /home/data, which App Service keeps across restarts and deploys.
set -e
cd "$(dirname "$0")"

if [ -z "$MATCHMAKER_DB" ] && [ -d /home/site ]; then
  export MATCHMAKER_DB=/home/data/matchmaker.db
fi
DB="${MATCHMAKER_DB:-data/matchmaker.db}"
mkdir -p "$(dirname "$DB")"

# First start: unpack the committed database snapshot so the site has data immediately.
if [ ! -f "$DB" ] && [ -f data/seed/matchmaker.db.gz ]; then
  echo "Unpacking database snapshot to $DB ..."
  gunzip -c data/seed/matchmaker.db.gz > "$DB"
fi

# Use the virtual environment App Service's build created, if there is one.
for venv in antenv .venv; do
  if [ -f "$venv/bin/activate" ]; then . "$venv/bin/activate"; break; fi
done

if [ "$MODE" = "job" ]; then
  exec python3 pipeline.py
fi
export REFRESH_HOURS="${REFRESH_HOURS:-24}"
exec python3 app.py
