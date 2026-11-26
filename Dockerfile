FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONFIG_DIR=/config \
    WEBUI_PORT=8095 \
    TZ=Europe/Brussels

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY VERSION .
COPY app/ ./app/

VOLUME /config
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WEBUI_PORT','8095')+'/healthz',timeout=5)"]

LABEL org.opencontainers.image.title="YTM Sync" \
      org.opencontainers.image.description="YouTube Music -> Navidrome -> SoulSync" \
      net.unraid.docker.webui="http://[IP]:[PORT:8095]/"

CMD ["python", "-m", "app.main"]
