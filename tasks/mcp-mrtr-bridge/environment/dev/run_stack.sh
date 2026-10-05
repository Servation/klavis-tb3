#!/usr/bin/env bash
# Development stack: the two dev legacy servers (tickets :9101, imports :9102),
# gateway instances a (:8101) and b (:8102) built from GATEWAY_DIR
# (default /app/gateway), and a round-robin load balancer on :8100 in front of
# them. Logs and pid files go to /tmp/mcp-dev.
#
#   run_stack.sh start | stop | restart | status
set -euo pipefail

DEV="$(cd "$(dirname "$0")" && pwd)"
GATEWAY_DIR="${GATEWAY_DIR:-/app/gateway}"
RUN=/tmp/mcp-dev

start() {
  mkdir -p "$RUN/state"
  [ -s "$RUN/state.key" ] || python3 -c 'import secrets; print(secrets.token_hex(32))' > "$RUN/state.key"
  export GATEWAY_CONFIG="$DEV/config.dev.json"
  export GATEWAY_STATE_DIR="$RUN/state"
  export GATEWAY_STATE_KEY="$(cat "$RUN/state.key")"
  export GATEWAY_STATE_TTL="${GATEWAY_STATE_TTL:-60}"

  python3 "$DEV/servers/tickets.py" --port 9101 --tokens "$DEV/upstream-tokens/tickets.json" > "$RUN/tickets.log" 2>&1 &
  echo $! > "$RUN/tickets.pid"
  python3 "$DEV/servers/imports.py" --port 9102 --tokens "$DEV/upstream-tokens/imports.json" > "$RUN/imports.log" 2>&1 &
  echo $! > "$RUN/imports.pid"
  for id in a b; do
    GATEWAY_INSTANCE="$id" bash "$GATEWAY_DIR/run.sh" > "$RUN/gateway-$id.log" 2>&1 &
    echo $! > "$RUN/gateway-$id.pid"
  done
  python3 "$DEV/lb.py" --listen 8100 --backend 127.0.0.1:8101 --backend 127.0.0.1:8102 > "$RUN/lb.log" 2>&1 &
  echo $! > "$RUN/lb.pid"

  for port in 8101 8102; do
    for _ in $(seq 1 50); do
      curl -fsS "http://127.0.0.1:$port/healthz" > /dev/null 2>&1 && break
      sleep 0.1
    done
  done
  status
}

stop() {
  for pid_file in "$RUN"/*.pid; do
    [ -e "$pid_file" ] || continue
    kill "$(cat "$pid_file")" 2> /dev/null || true
    rm -f "$pid_file"
  done
}

status() {
  for name in tickets imports gateway-a gateway-b lb; do
    if [ -e "$RUN/$name.pid" ] && kill -0 "$(cat "$RUN/$name.pid")" 2> /dev/null; then
      echo "$name: running (log $RUN/$name.log)"
    else
      echo "$name: stopped"
    fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 0.5; start ;;
  status) status ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
