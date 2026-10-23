#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${project_dir}/config.env"
if [[ $# -gt 0 ]]; then source "$1"; fi
exec /usr/bin/python3 "${project_dir}/scripts/warmup.py" --port "${RERANK_PORT}"
