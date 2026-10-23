#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]] \
    && [[ -x "${project_dir}/scripts/supervisor_stop.sh" ]]; then
    "${project_dir}/scripts/supervisor_stop.sh"
fi

stop_process() {
    local label="$1"
    local pid_file="$2"
    local expected="$3"

    [[ -s "${pid_file}" ]] || return 0
    local pid
    pid="$(<"${pid_file}")"
    if [[ ! "${pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi

    local command_line
    command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
    if [[ "${command_line}" != *"${expected}"* ]]; then
        echo "Refusing to stop PID ${pid}: it is not the expected ${label} process." >&2
        return 1
    fi

    kill -- "-${pid}" 2>/dev/null || kill "${pid}"
    for _ in $(seq 1 30); do
        if ! kill -0 "${pid}" 2>/dev/null; then
            echo "${label} stopped."
            return 0
        fi
        sleep 1
    done
    echo "${label} did not stop within 30 seconds." >&2
    return 1
}

stop_process "LightModel Gateway" "${project_dir}/runtime/gateway.pid" "quota_gateway.py"
backend_identity="${project_dir}/bin/llama-server"
if [[ -s "${project_dir}/runtime/backend.pid" ]]; then
    backend_pid="$(<"${project_dir}/runtime/backend.pid")"
    if [[ "${backend_pid}" =~ ^[0-9]+$ && -r "/proc/${backend_pid}/cmdline" ]] \
        && [[ "$(tr '\0' ' ' <"/proc/${backend_pid}/cmdline")" == *"${project_dir}/scripts/embedding_pool.py"* ]]; then
        backend_identity="${project_dir}/scripts/embedding_pool.py"
    fi
fi
stop_process "Local embedding backend" "${project_dir}/runtime/backend.pid" "${backend_identity}"
if [[ "${backend_identity}" == *"embedding_pool.py" ]]; then
    source "${project_dir}/config.pool.env"
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    worker_units=()
    for ((i=1; i<=EMBED_WORKER_COUNT; i++)); do
        worker_units+=("qwen3-embedding-worker@${i}.service")
    done
    systemctl --user stop "${worker_units[@]}"
fi
stop_process "Legacy embedding server" "${project_dir}/runtime/server.pid" "${project_dir}/bin/llama-server"

echo "LightModel Gateway is stopped."
