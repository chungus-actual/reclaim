FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RECLAIM_DATA=/config

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .

VOLUME /config
# the default; RECLAIM_PORT moves it (docker-compose.yml maps the same port)
EXPOSE 8892
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import sys, urllib.request, config as C; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{C.PORT}/api/health', timeout=4).status == 200 else 1)"

# listens on RECLAIM_HOST:RECLAIM_PORT (0.0.0.0:8892 unless set)
CMD ["python", "main.py"]
