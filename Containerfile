FROM docker.io/library/python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MIAS_CONFIG=/app/config.toml MIAS_DB_PATH=/data/mias.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mias/ mias/
COPY config.toml .

# Runs as any UID (OpenShift assigns a random one in group 0).
RUN mkdir -p /data && chgrp -R 0 /data /app && chmod -R g=u /data /app
USER 1001
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "mias", "run"]
