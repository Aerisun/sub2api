#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pid_file="${project_dir}/runtime/gateway.pid"

if [[ ! -s "${pid_file}" ]]; then
    echo "LightModel Gateway is not running."
    exit 0
fi

pid="$(<"${pid_file}")"
if [[ ! "${pid}" =~ ^[0-9]+$ ]] || ! kill -0 "${pid}" 2>/dev/null; then
    echo "LightModel Gateway is not running (stale PID file)."
    exit 0
fi
if [[ ! -r "/proc/${pid}/cmdline" ]]; then
    echo "Cannot validate LightModel Gateway PID ${pid}." >&2
    exit 1
fi
command_line="$(tr '\0' ' ' <"/proc/${pid}/cmdline")"
if [[ "${command_line}" != *"${project_dir}/scripts/quota_gateway.py"* ]]; then
    echo "Refusing to stop PID ${pid}: process identity does not match." >&2
    exit 1
fi

kill -- "-${pid}" 2>/dev/null || kill "${pid}"
for _ in $(seq 1 300); do
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "LightModel Gateway stopped."
        exit 0
    fi
    sleep 0.1
done

echo "LightModel Gateway did not stop in time." >&2
exit 1
