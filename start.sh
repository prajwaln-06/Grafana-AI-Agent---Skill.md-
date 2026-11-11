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
BACKEND_PID=$!

# The dev server proxies /api to the backend, and the browser hits that proxy
# as soon as the page is opened. The backend loads the skill package before
# it serves anything, which takes considerably longer than the sleep this
# script used to wait. Starting the frontend earlier only produced
# "http proxy error: /api/... ECONNREFUSED" for every request of the first
# page load, so the frontend is started only once the backend answers.
echo "⏳ Waiting for the backend on http://127.0.0.1:8008/readyz ..."
BACKEND_READY=0
for _ in $(seq 1 120); do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "❌ The backend exited during startup. Last lines of server.log:"
        tail -n 20 "$DIR/server.log"
        exit 1
    fi
    if curl -fsS "http://127.0.0.1:8008/readyz" >/dev/null 2>&1; then
        BACKEND_READY=1
        break
    fi
    sleep 1
done

if [ "$BACKEND_READY" -ne 1 ]; then
    echo "❌ The backend did not become ready within 120s. Last lines of server.log:"
    tail -n 20 "$DIR/server.log"
    exit 1
fi
echo "✅ Backend is ready."

echo "✨ Starting Frontend on http://localhost:${AGENT_UI_PORT} (dev server)..."
echo "   ➜ Open the app at http://localhost:${AGENT_UI_PORT}"
cd "$DIR/ui" && npm run dev
