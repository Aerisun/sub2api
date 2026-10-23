#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${project_dir}/artifacts" "${project_dir}/runtime"
if [[ ! -x "${project_dir}/bin/llama-server" ]]; then
  archive="${project_dir}/artifacts/llama-b11223-bin-ubuntu-x64.tar.gz"
  curl --fail --location --retry 3 --continue-at - \
    --proxy "${RERANK_DOWNLOAD_PROXY-http://127.0.0.1:17890}" \
    --output "${archive}" \
    https://github.com/ggml-org/llama.cpp/releases/download/b11223/llama-b11223-bin-ubuntu-x64.tar.gz
  printf '%s  %s\n' '9ce07ebd35ccdbae598ef34d21607564c4a21956cf075409f82e4f4fe871ea5e' "${archive}" | sha256sum --check --status
  extraction="$(mktemp -d "${project_dir}/artifacts/llama-extract.XXXXXX")"
  trap 'rm -rf -- "${extraction}"' EXIT
  tar -xzf "${archive}" -C "${extraction}"
  mv "${extraction}/llama-b11223" "${project_dir}/bin"
fi
python3 "${project_dir}/scripts/download_model.py"
"${project_dir}/bin/llama-server" --version
