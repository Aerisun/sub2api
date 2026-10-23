#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
source "${project_dir}/config.pool.env"
worker="${1:?worker index is required}"
[[ "${worker}" =~ ^[1-9][0-9]*$ ]]
(( worker <= EMBED_WORKER_COUNT && EMBED_WORKER_COUNT * EMBED_WORKER_PHYSICAL_CORES <= 32 ))
renice -n 15 -p $$ >/dev/null
if (( worker > EMBED_WORKER_MINIMUM )); then
    renice -n 19 -p $$ >/dev/null
fi
worker_cores="${EMBED_WORKER_PHYSICAL_CORES}"
(( EMBED_WORKER_THREADS <= worker_cores ))
worker_first=$(((worker - 1) * worker_cores))
worker_last=$((worker_first + worker_cores - 1))
worker_cpus="${worker_first}-${worker_last}"
worker_port=$((8110 + worker))
worker_mask="$(python3 - "${worker_cpus}" "${EMBED_WORKER_THREADS}" <<'PY'
import sys
lo, hi = map(int, sys.argv[1].split('-'))
cpus = list(range(lo, hi + 1))
if int(sys.argv[2]) > len(cpus):
    cpus += [cpu + 72 for cpu in cpus]
print(hex(sum(1 << cpu for cpu in cpus)))
PY
)"
if (( EMBED_WORKER_THREADS > worker_cores )); then
    worker_cpus="${worker_cpus},$((worker_first+72))-$((worker_last+72))"
fi
unset GGML_EMBED_PROFILE GGML_Q8_VNNI_256 GGML_Q8_CACHE_BLOCKED GGML_QWEN_RANK_ONLY
export GGML_QWEN_EMBED_ONLY=1
exec taskset -c "${worker_cpus}" "${EMBED_WORKER_BINARY}" \
  --model "${project_dir}/models/Qwen3-Embedding-4B-Q8_0.gguf" \
  --alias "${EMBED_MODEL_ALIAS}" --host 127.0.0.1 --port "${worker_port}" \
  --embedding --pooling last --device none --n-gpu-layers 0 --no-op-offload \
  --parallel "${EMBED_WORKER_PARALLEL}" --ctx-size "${EMBED_WORKER_CONTEXT}" \
  --threads "${EMBED_WORKER_THREADS}" --threads-batch "${EMBED_WORKER_THREADS}" \
  --cpu-mask "${worker_mask}" --cpu-strict 1 --cpu-mask-batch "${worker_mask}" --cpu-strict-batch 1 \
  --batch-size "${EMBED_WORKER_BATCH}" --ubatch-size "${EMBED_WORKER_UBATCH}" \
  --flash-attn on --cont-batching --poll 0 --poll-batch 0 --cache-ram 0 \
  --sleep-idle-seconds "${EMBED_WORKER_IDLE_SECONDS}" --load-mode "${EMBED_WORKER_LOAD_MODE:-none}" \
  --threads-http "${EMBED_WORKER_HTTP_THREADS}" --metrics --no-webui --no-slots --log-verbosity 2 \
  --api-key-file "${project_dir}/${EMBED_BACKEND_API_KEY_FILE}"
