#!/usr/bin/env python3
import csv
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

DATASET_PATH = os.environ.get("DATASET_PATH", "/dataset/demo_business_metrics.csv")
PORT = int(os.environ.get("PORT", "8000"))


def load_rows():
    with open(DATASET_PATH, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def latest_values():
    rows = load_rows()
    tick = int(time.time() / 15)
    services = sorted({row["service"] for row in rows})
    values = []

    for service in services:
        service_rows = [row for row in rows if row["service"] == service]
        row = service_rows[tick % len(service_rows)]
        labels = f'service="{service}"'
        values.extend(
            [
                f'demo_requests_total{{{labels}}} {row["requests"]}',
                f'demo_errors_total{{{labels}}} {row["errors"]}',
                f'demo_latency_ms{{{labels}}} {row["latency_ms"]}',
                f'demo_cpu_percent{{{labels}}} {row["cpu_percent"]}',
                f'demo_memory_mb{{{labels}}} {row["memory_mb"]}',
            ]
        )

    return "\n".join(values) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        body = latest_values().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
