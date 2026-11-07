# Federal Funds Matchmaker Dockerfile
# Compliant with Solveathon Reference Architecture (§4 Step 6 & 7)
# Single Docker image supporting two roles via MODE environment variable:
#   MODE=app -> starts interactive staff-facing web UI on port 8000
#   MODE=job -> runs the daily refresh ingestion/scoring pipeline and exits 0 on success

FROM python:3.11-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# curl: fallback for USAspending when Python's certificate bundle rejects it
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# pypdf (+ cryptography for encrypted PDFs) reads funding notices;
# anthropic is only used by `notices.py extract`
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files and data (the local database is rebuilt from public
# sources if it is missing; see .dockerignore for what is left out)
COPY pipeline.py app.py db.py notices.py /app/
COPY data/ /app/data/
COPY public/ /app/public/

# Default environment variables
ENV MODE=app
ENV PORT=8000

# Expose standard target port
EXPOSE 8000

# Single entrypoint supporting MODE=app and MODE=job
CMD ["sh", "-c", "if [ \"$MODE\" = \"job\" ]; then python3 pipeline.py; else python3 app.py; fi"]
