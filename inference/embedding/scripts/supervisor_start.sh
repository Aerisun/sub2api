#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
if [[ "${EMBED_BACKEND_MODE:-native}" == "pool" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    exec systemctl --user start qwen3-embedding.service
fi
pid_file="${project_dir}/runtime/supervisor.pid"
log_file="${project_dir}/runtime/supervisor.log"

supervisor_is_running() {
    [[ -s "${pid_file}" ]] || return 1
    local pid
    pid="$(<"${pid_file}")"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    [[ -r "/proc/${pid}/cmdline" ]] || return 1
    local command_line
    command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
    [[ "${command_line}" == *"${project_dir}/scripts/supervise.sh"* ]]
}

if supervisor_is_running; then
    echo "LightModel Gateway supervisor is already running."
    exit 0
fi

mkdir -p "${project_dir}/runtime"
nohup setsid "${project_dir}/scripts/supervise.sh" \
    >>"${log_file}" 2>&1 </dev/null &
launcher_pid=$!

for _ in $(seq 1 20); do
    if supervisor_is_running; then
        echo "LightModel Gateway supervisor started."
        exit 0
    fi
    if ! kill -0 "${launcher_pid}" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

echo "LightModel Gateway supervisor failed to start. See ${log_file}." >&2
exit 1
