FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY log_generator.py backfill.py ./
# topology.yaml and log_catalog.yaml are mounted at /config by docker-compose
ENV CONFIG_DIR=/config
CMD ["python3", "log_generator.py"]
