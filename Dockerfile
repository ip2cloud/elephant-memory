FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# /data precisa existir NA IMAGEM e ja pertencer ao usuario: um volume nomeado
# herda o dono do diretorio da imagem no momento em que e criado. Sem isto ele
# nasce root:root, e o processo (UID 10001) nao abre o SQLite do ledger —
# /ingest devolve 500 com "unable to open database file".
RUN useradd --system --uid 10001 --home /srv graphmem \
    && mkdir -p /data \
    && chown -R graphmem:graphmem /srv /data
USER 10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
