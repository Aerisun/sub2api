#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=/dev/null
source "${project_dir}/config.env"

runtime_dir="${project_dir}/runtime"
backend_key_file="${project_dir}/${EMBED_BACKEND_API_KEY_FILE}"
mkdir -p "${runtime_dir}"

# Do not queue another warm-up while a cold model is still paging in.
exec 9>"${runtime_dir}/warm_backend.lock"
flock -n 9 || exit 0

backend_key="$(tr -d '\r\n' <"${backend_key_file}")"
probe_id="$(date +%s%N)"
payload="{\"model\":\"${EMBED_MODEL_ALIAS}\",\"input\":\"model keepalive ${probe_id}\"}"

# This is a direct localhost request. It bypasses the public gateway, customer
# quotas, Caddy, Tailscale, and all HTTP/SOCKS proxy settings.
printf 'Authorization: Bearer %s\nContent-Type: application/json\n' "${backend_key}" |
    env \
        -u HTTP_PROXY \
        -u HTTPS_PROXY \
        -u ALL_PROXY \
        -u http_proxy \
        -u https_proxy \
        -u all_proxy \
        curl \
            --noproxy '*' \
            --fail \
            --silent \
            --show-error \
            --max-time 180 \
            --output /dev/null \
            --header @- \
            --data-binary "${payload}" \
            "http://${EMBED_BACKEND_HOST}:${EMBED_BACKEND_PORT}/v1/embeddings"
