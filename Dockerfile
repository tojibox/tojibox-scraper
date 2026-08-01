FROM python:3.13-slim

# psycopg2-binary's wheel dynamically links against libpq at runtime.
# python:3.13-slim doesn't include it, hence "ImportError: libpq.so.5:
# cannot open shared object file" without this. Same fix as tojibox-api.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: the scheduler (this repo's primary Railway service). If you also
# deploy main.py as an optional manual-trigger API service pointed at this
# same root directory, override its Start Command in Railway's dashboard to:
#   uvicorn main:app --host 0.0.0.0 --port $PORT
CMD ["python", "-m", "scrapers.scheduler"]
