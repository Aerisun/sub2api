#!/usr/bin/env bash
set -Eeuo pipefail
worker="${1:?worker index is required}"
[[ "${worker}" =~ ^[1-9][0-9]*$ ]]
for _ in $(seq 1 180); do
    if curl --fail --silent --max-time 1 "http://127.0.0.1:$((8110+worker))/health" >/dev/null; then
        exec /usr/bin/python3 /data/wenbo/services/qwen3-embedding-api/scripts/warm_worker.py "${worker}"
    fi
    sleep 1
done
exit 1
