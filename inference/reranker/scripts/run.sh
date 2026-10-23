#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
if [[ $# -gt 0 ]]; then source "$1"; fi
renice -n 15 -p $$ >/dev/null
if [[ "${RERANK_NICE:-15}" -gt 15 ]]; then
  renice -n "${RERANK_NICE}" -p $$ >/dev/null
fi
[[ "${RERANK_CPU_RANGE}" =~ ^([0-9]+)-([0-9]+)$ ]]
first_cpu="${BASH_REMATCH[1]}"
last_cpu="${BASH_REMATCH[2]}"
(( first_cpu <= last_cpu && last_cpu < 32 && RERANK_THREADS <= last_cpu - first_cpu + 1 ))
op_option=--no-op-offload
if [[ "${RERANK_CPU_BLAS:-0}" == 1 ]]; then op_option=--op-offload; fi
exec taskset -c "${RERANK_CPU_RANGE}" "${RERANK_SERVER_BINARY:-${project_dir}/bin/llama-server}" \
  --model "${project_dir}/models/Qwen3-Reranker-4B.Q8_0.gguf" \
  --alias "${RERANK_MODEL_ALIAS}" \
  --host "${RERANK_HOST}" --port "${RERANK_PORT}" \
  --rerank --embedding --pooling rank \
  --device none --n-gpu-layers 0 "${op_option}" \
  --parallel "${RERANK_PARALLEL}" --ctx-size "${RERANK_CONTEXT_SIZE}" \
  --threads "${RERANK_THREADS}" --threads-batch "${RERANK_THREADS}" \
  --cpu-range "${RERANK_CPU_RANGE}" --cpu-strict 1 \
  --cpu-range-batch "${RERANK_CPU_RANGE}" --cpu-strict-batch 1 \
  --batch-size "${RERANK_BATCH_SIZE}" --ubatch-size "${RERANK_UBATCH_SIZE}" \
  --flash-attn on --cont-batching --poll 0 --poll-batch 0 \
  --sleep-idle-seconds "${RERANK_SLEEP_IDLE_SECONDS:--1}" \
  --cache-ram "${RERANK_PROMPT_CACHE_MIB:-0}" \
  --load-mode "${RERANK_LOAD_MODE:-auto}" --threads-http 8 \
  --metrics --no-webui --no-slots --log-verbosity 1 \
  --api-key-file "${project_dir}/secrets/api_key.txt"
