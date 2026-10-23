#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="${project_dir}/bin"
model_dir="${project_dir}/models"
artifact_dir="${project_dir}/artifacts"

llama_release="b10549"
llama_archive="llama-${llama_release}-bin-ubuntu-x64.tar.gz"
llama_url="https://github.com/ggml-org/llama.cpp/releases/download/${llama_release}/${llama_archive}"
llama_sha256="66b26d8cb3ab8edaf5a12bfe642b8f00844925f614f196a96a222b7ed1582c1d"
model_file="Qwen3-Embedding-4B-Q8_0.gguf"
model_url="https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/main/${model_file}"
model_sha256="b60ae5ce2dd6a0b77f82cadf21def1f310a3e10cde380ad0081b07a9d416949d"

mkdir -p "${bin_dir}" "${model_dir}" "${artifact_dir}" "${project_dir}/runtime"

if [[ ! -x "${bin_dir}/llama-server" ]]; then
    archive_path="${artifact_dir}/${llama_archive}"
    curl --fail --location --retry 5 --continue-at - --output "${archive_path}" "${llama_url}"
    printf '%s  %s\n' "${llama_sha256}" "${archive_path}" | sha256sum --check --status
    temp_dir="$(mktemp -d "${artifact_dir}/llama-extract.XXXXXX")"
    trap 'rm -rf -- "${temp_dir}"' EXIT
    tar -xzf "${archive_path}" -C "${temp_dir}"
    server_path="$(find "${temp_dir}" -type f -name llama-server -print -quit)"
    if [[ -z "${server_path}" ]]; then
        echo "llama-server was not found in ${llama_archive}" >&2
        exit 1
    fi
    server_parent="$(dirname "${server_path}")"
    cp -a "${server_parent}/." "${bin_dir}/"
    chmod u+x "${bin_dir}/llama-server"
fi

model_path="${model_dir}/${model_file}"
model_part="${model_path}.part"
if ! printf '%s  %s\n' "${model_sha256}" "${model_path}" | sha256sum --check --status 2>/dev/null; then
    if [[ -s "${model_path}" && ! -e "${model_part}" ]]; then
        mv "${model_path}" "${model_part}"
    fi
    curl --fail --location --retry 5 --continue-at - --output "${model_part}" "${model_url}"
    printf '%s  %s\n' "${model_sha256}" "${model_part}" | sha256sum --check --status
    mv "${model_part}" "${model_path}"
fi

"${bin_dir}/llama-server" --version
ls -lh "${model_path}"
