#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
if [[ "${EMBED_BACKEND_MODE:-native}" == "pool" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    exec systemctl --user stop qwen3-embedding.service
fi
pid_file="${project_dir}/runtime/supervisor.pid"

if [[ ! -s "${pid_file}" ]]; then
    echo "LightModel Gateway supervisor is not running."
    exit 0
fi

pid="$(<"${pid_file}")"
if [[ ! "${pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${pid}" 2>/dev/null; then
    echo "LightModel Gateway supervisor is not running (stale PID file)."
    exit 0
fi
if [[ ! -r "/proc/${pid}/cmdline" ]]; then
    echo "Cannot validate LightModel Gateway supervisor PID ${pid}." >&2
    exit 1
fi
command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
if [[ "${command_line}" != *"${project_dir}/scripts/supervise.sh"* ]]; then
    echo "Refusing to stop PID ${pid}: process identity does not match." >&2
    exit 1
fi

kill -- "-${pid}" 2>/dev/null || kill "${pid}"
for _ in $(seq 1 30); do
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "LightModel Gateway supervisor stopped."
        exit 0
    fi
    sleep 0.1
done

echo "LightModel Gateway supervisor did not stop in time." >&2
exit 1
