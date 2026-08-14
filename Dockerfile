FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py .
COPY incidents/ ./incidents/

ENV NODE_ID=node-01 \
    PORT=9200 \
    NUM_CPUS=32 \
    NUM_GPUS=8 \
    MEM_TOTAL_GB=256 \
    SWAP_TOTAL_GB=16 \
    NUM_FILESYSTEMS=3 \
    TICK_SECONDS=2 \
    CONTROLLER_URL=http://incident-controller:9500

EXPOSE 9200

# GET /metrics must return 200 (spec section 4). Uses python3 (already
# the base image) instead of curl/wget, which python:3.11-slim doesn't
# ship with.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD python3 -c "import os,urllib.request as u; u.urlopen('http://localhost:'+os.environ.get('PORT','9200')+'/metrics', timeout=2)" || exit 1

CMD ["python3", "exporter.py"]
