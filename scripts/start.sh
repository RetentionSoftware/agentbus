#!/bin/sh
# Starts the agent bus for a project as a background process that outlives the terminal and every Claude session.
#
#   scripts/start.sh [project-dir]
#
# The database, the logs and the server's pid file live in <project-dir>/.agentbus/ (default: the current directory).
# Runs the agentbus checkout this script belongs to, through uv. Port: $AGENTBUS_PORT, else 8765; the project name
# and protocol path come from AGENTBUS_PROJECT_NAME and AGENTBUS_PROTOCOL when set. Monitor: `agentbus watch`.
set -eu

package=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
project=$(CDPATH='' cd -- "${1:-.}" && pwd)
port=${AGENTBUS_PORT:-8765}
state="$project/.agentbus"
mkdir -p "$state"

agentbus() {
    uv run --quiet --project "$package" agentbus --port "$port" "$@"
}

if health=$(agentbus health 2>/dev/null); then
    echo "agentbus already running on port $port:"
    echo "$health"
    exit 0
fi

cd "$project"
nohup uv run --quiet --project "$package" agentbus --port "$port" serve --db "$state/bus.db" \
    >"$state/bus.log" 2>"$state/bus.err.log" </dev/null &

i=0
while [ "$i" -lt 60 ]; do
    sleep 0.5
    if agentbus health >/dev/null 2>&1; then
        echo "agentbus started: http://127.0.0.1:$port/mcp, pid $(cat "$state/server.pid"), logs in $state"
        exit 0
    fi
    i=$((i + 1))
done
echo "agentbus did not start; see $state/bus.err.log" >&2
exit 1
