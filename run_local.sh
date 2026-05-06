#!/usr/bin/env bash
set -euo pipefail

# --- Load .env ---
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

PORT="${PORT:-9999}"

# --- Start the agent server in the background ---
echo "==> Starting Clinical Decision Room on port ${PORT}..."
python -m clinical_decision_room &
SERVER_PID=$!

# Give the server a moment to bind
sleep 2

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
  echo "ERROR: Server failed to start."
  exit 1
fi

# --- Start ngrok ---
if [ -n "${NGROK_URL:-}" ]; then
  echo "==> Starting ngrok tunnel: https://${NGROK_URL} -> localhost:${PORT}"
  ngrok http "$PORT" --url="$NGROK_URL" &
else
  echo "==> Starting ngrok tunnel (ephemeral URL) -> localhost:${PORT}"
  ngrok http "$PORT" &
fi
NGROK_PID=$!

# --- Cleanup on exit ---
cleanup() {
  echo ""
  echo "==> Shutting down..."
  kill "$SERVER_PID" 2>/dev/null
  kill "$NGROK_PID" 2>/dev/null
  wait "$SERVER_PID" 2>/dev/null
  wait "$NGROK_PID" 2>/dev/null
  echo "==> Done."
}
trap cleanup EXIT INT TERM

echo ""
echo "=== Running ==="
if [ -n "${NGROK_URL:-}" ]; then
  echo "  Agent card: https://${NGROK_URL}/.well-known/agent-card.json"
fi
echo "  Local:      http://localhost:${PORT}"
echo ""
echo "Press Ctrl+C to stop."
echo ""

wait
