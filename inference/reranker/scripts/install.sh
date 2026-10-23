#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
source "${project_dir}/config.env"
[[ -x "${RERANK_SERVER_BINARY:-${project_dir}/bin/llama-server}" ]]
[[ -s "${project_dir}/models/Qwen3-Reranker-4B.Q8_0.gguf" ]]
mkdir -p "${project_dir}/secrets" "${HOME}/.config/systemd/user"
if [[ ! -s "${project_dir}/secrets/api_key.txt" ]]; then
  umask 077
  python3 - "${project_dir}/secrets/api_key.txt" <<'PY'
from pathlib import Path
import secrets, sys
path = Path(sys.argv[1])
path.write_text('sk-rerank-' + secrets.token_urlsafe(32) + '\n')
path.chmod(0o600)
PY
fi
chmod u+x "${project_dir}/scripts/run.sh"
install -m 600 "${project_dir}/systemd/localinference.slice" "${HOME}/.config/systemd/user/localinference.slice"
install -m 600 "${project_dir}/systemd/rerank.slice" "${HOME}/.config/systemd/user/localinference-rerank.slice"
install -m 600 "${project_dir}/qwen3-reranker.service" "${HOME}/.config/systemd/user/qwen3-reranker.service"
install -m 600 "${project_dir}/systemd/qwen3-reranker@.service" "${HOME}/.config/systemd/user/qwen3-reranker@.service"
install -m 600 "${project_dir}/systemd/qwen3-reranker-pool.service" "${HOME}/.config/systemd/user/qwen3-reranker-pool.service"
systemctl --user daemon-reload
systemctl --user disable --now qwen3-reranker@3.service qwen3-reranker@4.service
systemctl --user disable qwen3-reranker@2.service
systemctl --user enable --now qwen3-reranker.service qwen3-reranker-pool.service
for ((attempt=0; attempt<120; attempt++)); do
  if curl --noproxy '*' --fail --silent --max-time 1 "http://127.0.0.1:8101/health" >/dev/null; then
    printf 'Reranker pool ready: http://127.0.0.1:8101/v1/rerank\n'
    exit 0
  fi
  if systemctl --user is-failed --quiet qwen3-reranker.service; then
    journalctl --user -u qwen3-reranker.service -n 30 --no-pager
    exit 1
  fi
  sleep 1
done
echo 'Reranker did not become healthy; inspect the service journal.' >&2
exit 1
