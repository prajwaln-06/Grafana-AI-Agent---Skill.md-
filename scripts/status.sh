#!/usr/bin/env sh
set -eu

echo "== Containers =="
docker compose ps

echo
echo "== Grafana health =="
curl -s http://localhost:3000/api/health

echo
echo
echo "== Prometheus health =="
curl -s http://localhost:9090/-/healthy

echo
echo
echo "== Loki readiness =="
curl -s http://localhost:3100/ready

echo
echo
echo "== Grafana dashboards =="
curl -s -u admin:admin http://localhost:3000/api/search

echo
echo
echo "== Recent dummy log counts =="
curl -s -G http://localhost:3100/loki/api/v1/query \
  --data-urlencode 'query=count_over_time({container="log-generator-ai-lab"}[5m])'

echo
echo
echo "== Demo dataset exporter =="
curl -s http://localhost:8000/metrics

echo
echo
echo "== Demo dataset Prometheus query =="
curl -s -G http://localhost:9090/api/v1/query \
  --data-urlencode 'query=demo_requests_total'

echo
