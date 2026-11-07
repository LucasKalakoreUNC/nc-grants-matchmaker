# NC Grants Matchmaker
# One image, two roles (MODE environment variable):
#   MODE=app (default) -> serves the site on $PORT (8000) and refreshes data
#                          every $REFRESH_HOURS hours (default 24)
#   MODE=job           -> runs the data refresh once and exits

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl: fallback for USAspending when Python's certificate bundle rejects it
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# pypdf + cryptography read funding notices and plans; anthropic is used by ai.py / notices.py extract
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Every Python module (app, pipeline, db, notices, ai, programs, state_programs) and the start script
COPY *.py start.sh /app/
COPY public/ /app/public/
# Profiles, requirement files, plan text, raw Grants.gov data and the database
# snapshot (data/seed/matchmaker.db.gz, unpacked on first start by start.sh)
COPY data/ /app/data/

ENV MODE=app \
    PORT=8000 \
    REFRESH_HOURS=24

EXPOSE 8000

CMD ["sh", "/app/start.sh"]
