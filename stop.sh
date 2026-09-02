#!/usr/bin/env bash
echo "🛑 Stopping Grafana AI servers..."
lsof -t -i :8008 -i :5173 | xargs kill -9 2>/dev/null || true
echo "✅ All servers stopped."
