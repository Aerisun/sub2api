#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${project_dir}/config.env"
if [[ "${EMBED_BACKEND_MODE:-native}" == "pool" && "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]]; then
    export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
    systemctl --user start qwen3-embedding.service
    # The existing supervisor recovers this gateway inside its memory/CPU group.
    for _ in $(seq 1 45); do
        if curl --fail --silent --max-time 1 "http://${EMBED_HOST}:${EMBED_PORT}/health" >/dev/null; then
            exit 0
        fi
        sleep 1
    done
    echo "The managed gateway did not recover in time." >&2
    exit 1
fi

pid_file="${project_dir}/runtime/gateway.pid"
log_file="${project_dir}/runtime/gateway.log"

if [[ -s "${pid_file}" ]]; then
    pid="$(<"${pid_file}")"
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
        command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
        if [[ "${command_line}" == *"${project_dir}/scripts/quota_gateway.py"* ]]; then
            echo "LightModel Gateway is already running."
            exit 0
        fi
        echo "Gateway PID file refers to an unexpected live process." >&2
        exit 1
    fi
fi

if command -v ss >/dev/null \
    && ss -lnt "sport = :${EMBED_PORT}" | tail -n +2 | grep -q .; then
    echo "Gateway port ${EMBED_PORT} is already in use." >&2
    exit 1
fi

nohup setsid python3 "${project_dir}/scripts/quota_gateway.py" \
    >"${log_file}" 2>&1 </dev/null &
pid=$!
printf '%s\n' "${pid}" >"${pid_file}"

for _ in $(seq 1 90); do
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "LightModel Gateway exited during startup. See ${log_file}." >&2
        tail -n 80 "${log_file}" >&2 || true
        exit 1
    fi
    if curl --fail --silent --max-time 6 \
        "http://${EMBED_HOST}:${EMBED_PORT}/health" >/dev/null; then
        echo "LightModel Gateway started."
        exit 0
    fi
    sleep 1
done

echo "LightModel Gateway did not become healthy in time. See ${log_file}." >&2
kill -- "-${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
exit 1
