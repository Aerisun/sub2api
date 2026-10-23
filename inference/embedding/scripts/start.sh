#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${project_dir}/config.env"

server_bin="${project_dir}/bin/llama-server"
model_path="${project_dir}/models/Qwen3-Embedding-4B-Q8_0.gguf"
backend_key_file="${project_dir}/${EMBED_BACKEND_API_KEY_FILE}"
backend_pid_file="${project_dir}/runtime/backend.pid"
gateway_pid_file="${project_dir}/runtime/gateway.pid"
legacy_pid_file="${project_dir}/runtime/server.pid"
backend_log="${project_dir}/runtime/backend.log"

mkdir -p "${project_dir}/runtime" "$(dirname "${backend_key_file}")"

if [[ ! -x "${server_bin}" || ! -s "${model_path}" ]]; then
    echo "Runtime or model is missing. Run ./scripts/bootstrap.sh first." >&2
    exit 1
fi

if [[ ! -s "${backend_key_file}" ]]; then
    umask 077
    temporary_key_file="${backend_key_file}.new"
    printf 'internal_%s\n' "$(openssl rand -hex 32)" >"${temporary_key_file}"
    chmod 600 "${temporary_key_file}"
    mv "${temporary_key_file}" "${backend_key_file}"
fi

"${project_dir}/scripts/keyctl.py" init >/dev/null

if [[ "${EMBED_BACKEND_MODE:-native}" == "pool" ]]; then
    exec "${project_dir}/scripts/start_pool.sh"
fi

pid_is_running() {
    local pid_file="$1"
    [[ -s "${pid_file}" ]] || return 1
    local pid
    pid="$(<"${pid_file}")"
    [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null
}

gateway_running=0
backend_running=0
pid_is_running "${gateway_pid_file}" && gateway_running=1
pid_is_running "${backend_pid_file}" && backend_running=1

if ((gateway_running == 1 && backend_running == 1)); then
    echo "LightModel Gateway is already running."
    if [[ "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]]; then
        "${project_dir}/scripts/supervisor_start.sh"
    fi
    exit 0
fi

if ((gateway_running == 0 && backend_running == 1)) \
    && curl --fail --silent --max-time 5 \
        "http://${EMBED_BACKEND_HOST}:${EMBED_BACKEND_PORT}/health" >/dev/null; then
    "${project_dir}/scripts/gateway_start.sh"
    echo "LightModel Gateway recovered without reloading the local model backend."
    if [[ "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]]; then
        "${project_dir}/scripts/supervisor_start.sh"
    fi
    exit 0
fi

if ((gateway_running == 1 || backend_running == 1)) \
    || pid_is_running "${legacy_pid_file}"; then
    echo "A partial or legacy service is running. Run ./scripts/stop.sh first." >&2
    exit 1
fi

for port in "${EMBED_PORT}" "${EMBED_BACKEND_PORT}"; do
    if command -v ss >/dev/null && ss -lnt "sport = :${port}" | tail -n +2 | grep -q .; then
        echo "Port ${port} is already in use." >&2
        exit 1
    fi
done

nohup setsid "${server_bin}" \
    --model "${model_path}" \
    --alias "${EMBED_MODEL_ALIAS}" \
    --host "${EMBED_BACKEND_HOST}" \
    --port "${EMBED_BACKEND_PORT}" \
    --embedding \
    --pooling last \
    --parallel "${EMBED_PARALLEL}" \
    --ctx-size "${EMBED_CONTEXT_SIZE}" \
    --threads "${EMBED_THREADS}" \
    --threads-batch "${EMBED_THREADS}" \
    --batch-size "${EMBED_BATCH_SIZE}" \
    --ubatch-size "${EMBED_UBATCH_SIZE}" \
    --cont-batching \
    --metrics \
    --log-verbosity "${EMBED_LOG_VERBOSITY}" \
    --api-key-file "${backend_key_file}" \
    >"${backend_log}" 2>&1 </dev/null &

backend_pid=$!
printf '%s\n' "${backend_pid}" >"${backend_pid_file}"

for _ in $(seq 1 180); do
    if ! kill -0 "${backend_pid}" 2>/dev/null; then
        echo "Local embedding backend exited during startup. See ${backend_log}" >&2
        tail -n 80 "${backend_log}" >&2 || true
        exit 1
    fi
    if curl --fail --silent --max-time 1 "http://${EMBED_BACKEND_HOST}:${EMBED_BACKEND_PORT}/health" >/dev/null; then
        break
    fi
    sleep 1
done

if ! curl --fail --silent --max-time 2 "http://${EMBED_BACKEND_HOST}:${EMBED_BACKEND_PORT}/health" >/dev/null; then
    echo "Local embedding backend did not become healthy in time." >&2
    kill -- "-${backend_pid}" 2>/dev/null || kill "${backend_pid}" 2>/dev/null || true
    exit 1
fi

if ! "${project_dir}/scripts/gateway_start.sh"; then
    kill -- "-${backend_pid}" 2>/dev/null || kill "${backend_pid}" 2>/dev/null || true
    exit 1
fi

gateway_pid="$(<"${gateway_pid_file}")"
echo "LightModel Gateway is ready at http://${EMBED_HOST}:${EMBED_PORT}."
echo "Gateway PID ${gateway_pid}; backend PID ${backend_pid}."
if [[ "${EMBED_FROM_SUPERVISOR:-0}" != "1" ]]; then
    "${project_dir}/scripts/supervisor_start.sh"
fi
