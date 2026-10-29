FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py .

ENV NODE_ID=node-01 \
    PORT=9200 \
    NUM_CPUS=32 \
    NUM_GPUS=8 \
    MEM_TOTAL_GB=256 \
    SWAP_TOTAL_GB=16 \
    NUM_FILESYSTEMS=3 \
    TICK_SECONDS=2

EXPOSE 9200
CMD ["python3", "exporter.py"]
