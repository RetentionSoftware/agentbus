#!/bin/sh
# Stops the agent bus of a project. The state stays in <project-dir>/.agentbus/bus.db.
#
#   scripts/stop.sh [project-dir]
#
# Stops exactly the process recorded in <project-dir>/.agentbus/server.pid, and only if it is an agentbus process.
set -eu

project=$(CDPATH='' cd -- "${1:-.}" && pwd)
pidfile="$project/.agentbus/server.pid"

if [ ! -f "$pidfile" ]; then
    echo "agentbus is not running for this project (no $pidfile)"
    exit 0
fi
pid=$(cat "$pidfile")
if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pidfile"
    echo "agentbus was not running (removed stale pid file, pid $pid)"
    exit 0
fi
case "$(ps -p "$pid" -o args= 2>/dev/null || true)" in
    *agentbus*) ;;
    *)
        echo "pid $pid is not an agentbus process, not stopping it" >&2
        exit 1
        ;;
esac

kill "$pid"  # SIGTERM: the server shuts down cleanly and removes its pid file
i=0
while kill -0 "$pid" 2>/dev/null; do
    if [ "$i" -ge 50 ]; then
        echo "agentbus (pid $pid) did not stop within 10 s" >&2
        exit 1
    fi
    sleep 0.2
    i=$((i + 1))
done
rm -f "$pidfile"
echo "stopped agentbus (pid $pid)"
