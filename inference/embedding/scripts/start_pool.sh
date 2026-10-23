#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
source "${project_dir}/config.pool.env"
worker_ports=()
for ((i=1; i<=EMBED_WORKER_COUNT; i++)); do
    worker_ports+=("$((8110+i))")
done
workers="$(IFS=,; echo "${worker_ports[*]}")"
pool_command=(python3 "${project_dir}/scripts/embedding_pool.py"
    --port "${EMBED_POOL_PORT}" --workers "${workers}"
    --concurrency "${EMBED_POOL_CONCURRENCY}" --per-worker "${EMBED_POOL_WORKER_CONCURRENCY}"
    --capacity "${EMBED_POOL_ADMISSION}" --handlers "${EMBED_POOL_HTTP_HANDLERS}"
    --upstream-timeout "${EMBED_POOL_UPSTREAM_TIMEOUT}"
    --scheduler "${EMBED_POOL_SCHEDULER:-fifo}" --aging-seconds "${EMBED_POOL_AGING_SECONDS:-15}"
    --max-bypass "${EMBED_POOL_MAX_BYPASS:-4}" --short-cost-limit "${EMBED_POOL_SHORT_COST_LIMIT:-512}"
    --elastic-config "${project_dir}/config.elastic.json")
if [[ "${1:-}" == "--print-command" ]]; then
    python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${pool_command[@]}"
    exit 0
fi
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
if [[ "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]]; then
    systemctl --user start qwen3-embedding.service
    exit 0
fi
pid_file="${project_dir}/runtime/backend.pid"
pool_running=0
if [[ -s "${pid_file}" ]]; then
    pool_pid="$(<"${pid_file}")"
    if [[ "${pool_pid}" =~ ^[0-9]+$ ]] && kill -0 "${pool_pid}" 2>/dev/null; then
        command_line="$(tr '\0' ' ' <"/proc/${pool_pid}/cmdline")"
        [[ "${command_line}" == *"${project_dir}/scripts/embedding_pool.py"* ]] || {
            echo "Port 8091 still has the previous backend; drain and stop it before switching." >&2
            exit 1
        }
        pool_running=1
    fi
fi
worker_units=()
for ((i=1; i<=EMBED_WORKER_COUNT; i++)); do
    if (( i <= EMBED_WORKER_MINIMUM )); then
        worker_units+=("qwen3-embedding-worker@${i}.service")
    fi
done
systemctl --user start "${worker_units[@]}"
if (( pool_running == 0 )); then
    if ss -lnt "sport = :${EMBED_POOL_PORT}" | tail -n +2 | grep -q .; then
        echo "The embedding dispatch port is already in use." >&2
        exit 1
    fi
    nohup setsid "${pool_command[@]}" \
        >"${project_dir}/runtime/embedding-pool.log" 2>&1 </dev/null &
    pool_pid=$!
    printf '%s\n' "${pool_pid}" >"${pid_file}"
fi
for _ in $(seq 1 30); do
    if curl --fail --silent --max-time 1 "http://127.0.0.1:${EMBED_POOL_PORT}/health" >/dev/null; then
        "${project_dir}/scripts/gateway_start.sh"
        if [[ "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]]; then
            "${project_dir}/scripts/supervisor_start.sh"
        fi
        echo "CPU embedding pool is ready: ${EMBED_POOL_ADMISSION} admitted requests, ${EMBED_WORKER_MINIMUM}-${EMBED_WORKER_COUNT} elastic workers."
        exit 0
    fi
    kill -0 "${pool_pid}" 2>/dev/null || break
    sleep 1
done
echo "Embedding dispatcher startup failed; see runtime/embedding-pool.log." >&2
exit 1
