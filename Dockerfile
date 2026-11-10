FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && install -d -m 0750 -o app -g app /data

WORKDIR /app
COPY requirements-runtime.txt .
RUN python -m pip install --no-cache-dir -r requirements-runtime.txt gunicorn==26.2.0

COPY app.py .
COPY election_system/ election_system/

USER app
EXPOSE 8000
CMD ["sh", "-ec", "python app.py --init-db && exec gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 2 app:app"]
