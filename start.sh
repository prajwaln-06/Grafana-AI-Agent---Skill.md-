#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Clean any existing processes on 8008 and 5173
lsof -t -i :8008 -i :5173 | xargs kill -9 2>/dev/null || true

echo "🚀 Starting Backend (port 8008)..."
PORT=8008 API_PORT=8008 PYTHONUNBUFFERED=1 "$DIR/.venv/bin/python" run_server.py > "$DIR/server.log" 2>&1 &

sleep 2

echo "✨ Starting Frontend on http://localhost:5173..."
cd "$DIR/ui" && npm run dev
