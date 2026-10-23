#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${project_dir}/config.env"

show_process() {
    local label="$1"
    local pid_file="$2"
    if [[ ! -s "${pid_file}" ]]; then
        echo "${label}: not running"
        return 1
    fi
    local pid
    pid="$(<"${pid_file}")"
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
        echo "${label}:"
        ps -p "${pid}" -o pid=,stat=,%cpu=,%mem=,rss=,etime=,cmd=
    else
        echo "${label}: not running (stale PID file)"
        return 1
    fi
}

healthy=0
show_process "Service supervisor" "${project_dir}/runtime/supervisor.pid" || healthy=1
show_process "LightModel Gateway" "${project_dir}/runtime/gateway.pid" || healthy=1
show_process "Local embedding backend" "${project_dir}/runtime/backend.pid" || healthy=1

if ! curl --fail --silent --max-time 2 "http://${EMBED_HOST}:${EMBED_PORT}/health"; then
    healthy=1
fi
printf '\n'
exit "${healthy}"
