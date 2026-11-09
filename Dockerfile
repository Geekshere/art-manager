# Art Manager — container image for homelab use.
#   docker build -t art-manager .
#   docker run -p 5000:5000 -v /path/to/Music:/music -v art-manager-data:/data \
#              -e ARTMGR_PASSWORD=choose-one art-manager /music
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ARTMGR_DATA_DIR=/data ARTMGR_HOST=0.0.0.0 ARTMGR_PORT=5000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY art_manager ./art_manager
RUN pip install --no-cache-dir . && useradd --uid 1000 --create-home artmgr \
    && mkdir -p /data && chown artmgr /data

USER artmgr
VOLUME ["/data"]
EXPOSE 5000
ENTRYPOINT ["art-manager"]
