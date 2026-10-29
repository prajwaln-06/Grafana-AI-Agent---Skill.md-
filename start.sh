#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Clean any existing processes on 8008 (backend) and 5173 (vite dev server).
lsof -t -i :8008 -i :5173 | xargs kill -9 2>/dev/null || true

# The vite dev server publishes the application on :5173 -- the one normal
# application URL -- and proxies /api to the backend on :8008. The port can
# be overridden with AGENT_UI_PORT.
export AGENT_UI_PORT="${AGENT_UI_PORT:-5173}"

echo "🚀 Starting Backend (port 8008)..."
PORT=8008 API_PORT=8008 PYTHONUNBUFFERED=1 "$DIR/.venv/bin/python" run_server.py > "$DIR/server.log" 2>&1 &

sleep 2

echo "✨ Starting Frontend on http://localhost:${AGENT_UI_PORT} (dev server)..."
echo "   ➜ Open the app at http://localhost:${AGENT_UI_PORT}"
cd "$DIR/ui" && npm run dev
