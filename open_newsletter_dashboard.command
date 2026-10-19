#!/bin/bash
set -e
project_dir="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$project_dir/issues"
if /usr/bin/curl --max-time 1 -fsS http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
  /usr/bin/open http://127.0.0.1:8765/
  exit 0
fi
cd "$project_dir"
.venv/bin/python -u newsletter_app.py >> issues/dashboard.log 2>&1 &
server_pid=$!
trap 'kill "$server_pid" >/dev/null 2>&1 || true' EXIT
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if /usr/bin/curl --max-time 1 -fsS http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
    /usr/bin/open http://127.0.0.1:8765/
    wait "$server_pid"
    exit 0
  fi
  /bin/sleep 1
done
echo "AI Pulse dashboard did not start. Check issues/dashboard.log."
exit 1
