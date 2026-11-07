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

# Copy application files and pre-seeded data contracts
COPY pipeline.py app.py /app/
COPY data/ /app/data/
COPY public/ /app/public/

# Default environment variables
ENV MODE=app
ENV PORT=8000

# Expose standard target port
EXPOSE 8000

# Single entrypoint supporting MODE=app and MODE=job
CMD ["sh", "-c", "if [ \"$MODE\" = \"job\" ]; then python3 pipeline.py; else python3 app.py; fi"]
