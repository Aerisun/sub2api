#!/usr/bin/env bash
set -Euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${project_dir}/config.env"

runtime_dir="${project_dir}/runtime"
lock_file="${runtime_dir}/supervisor.lock"
pid_file="${runtime_dir}/supervisor.pid"
gateway_pid_file="${runtime_dir}/gateway.pid"
backend_pid_file="${runtime_dir}/backend.pid"
check_interval="${EMBED_SUPERVISOR_CHECK_INTERVAL_SECONDS:-15}"
failure_threshold="${EMBED_SUPERVISOR_FAILURE_THRESHOLD:-3}"
backend_identity="${project_dir}/bin/llama-server"
if [[ "${EMBED_BACKEND_MODE:-native}" == "pool" ]]; then
    backend_identity="${project_dir}/scripts/embedding_pool.py"
fi

if [[ ! "${check_interval}" =~ ^[1-9][0-9]*$ ]] \
    || [[ ! "${failure_threshold}" =~ ^[1-9][0-9]*$ ]]; then
    echo "Supervisor interval and failure threshold must be positive integers." >&2
    exit 1
fi

mkdir -p "${runtime_dir}"
exec 9>"${lock_file}"
if ! flock -n 9; then
    echo "LightModel Gateway supervisor is already running."
    exit 0
fi
printf '%s\n' "$$" >"${pid_file}"

log() {
    printf '%(%Y-%m-%dT%H:%M:%SZ)T %s\n' -1 "$*"
}

process_matches() {
    local candidate_pid_file="$1"
    local expected="$2"
    [[ -s "${candidate_pid_file}" ]] || return 1
    local candidate_pid
    candidate_pid="$(<"${candidate_pid_file}")"
    [[ "${candidate_pid}" =~ ^[0-9]+$ ]] || return 1
    kill -0 "${candidate_pid}" 2>/dev/null || return 1
    [[ -r "/proc/${candidate_pid}/cmdline" ]] || return 1
    local command_line
    command_line="$(tr '\0' ' ' <"/proc/${candidate_pid}/cmdline")"
    [[ "${command_line}" == *"${expected}"* ]]
}

restart_service() {
    log "restarting LightModel Gateway service"
    EMBED_FROM_SUPERVISOR=1 "${project_dir}/scripts/stop.sh" 9>&- || true
    if EMBED_FROM_SUPERVISOR=1 "${project_dir}/scripts/start.sh" 9>&-; then
        log "LightModel Gateway service recovered"
        return 0
    fi
    log "LightModel Gateway service restart failed; will retry"
    return 1
}

restart_gateway() {
    log "restarting LightModel Gateway without reloading the local model"
    EMBED_FROM_SUPERVISOR=1 "${project_dir}/scripts/gateway_stop.sh" 9>&- \
        || return 1
    if EMBED_FROM_SUPERVISOR=1 "${project_dir}/scripts/gateway_start.sh" 9>&-; then
        log "LightModel Gateway recovered"
        return 0
    fi
    log "LightModel Gateway restart failed; will retry"
    return 1
}

backend_is_healthy() {
    curl --fail --silent --max-time 5 \
        "http://${EMBED_BACKEND_HOST}:${EMBED_BACKEND_PORT}/health" >/dev/null
}

health_failures=0
log "LightModel Gateway supervisor started"

while true; do
    gateway_running=0
    backend_running=0
    process_matches "${gateway_pid_file}" "quota_gateway.py" && gateway_running=1
    process_matches "${backend_pid_file}" "${backend_identity}" \
        && backend_running=1

    if ((gateway_running == 0 && backend_running == 1)) && backend_is_healthy; then
        health_failures=0
        restart_gateway || true
    elif ((gateway_running == 0 || backend_running == 0)); then
        health_failures=0
        restart_service || true
    elif curl --fail --silent --max-time 5 \
        "http://${EMBED_HOST}:${EMBED_PORT}/health" >/dev/null; then
        health_failures=0
    else
        ((health_failures += 1))
        log "health check failed (${health_failures}/${failure_threshold})"
        if ((health_failures >= failure_threshold)); then
            health_failures=0
            if backend_is_healthy; then
                restart_gateway || true
            else
                restart_service || true
            fi
        fi
    fi

    sleep "${check_interval}"
done
