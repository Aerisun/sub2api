#!/usr/bin/env bash
set -Eeuo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${project_dir}"
printf '%s  %s\n' \
  e071d16a5c318673f909b19c9dde4f5e3a71aa55b3a5bd8164047675367eef59 source/llama-b11223.tar.gz \
  eb453dc99092d49855bda9ca04faeba0a45772dda08afb5fc47c17050dfe4650 patches/vnni-pr-30036.diff \
  6112cbee30cf5412defa912a0285ad39e9fec36b13deee9338fd161fac2d2288 patches/qwen-rank-only-experiment.patch \
  102ac400d68f02877c2fdfecf0732872652298448352b6faf28ec7f8dca8c913 models/Qwen3-Reranker-4B.Q8_0.gguf \
  | sha256sum --check --status
if [[ ! -x .venv-build/bin/cmake ]]; then
  uv venv .venv-build --python /usr/bin/python3
  uv pip install --python .venv-build/bin/python -r build-requirements.txt
fi
source_dir=source/llama-vnni-30036
if [[ ! -d "${source_dir}" ]]; then
  mkdir -p "${source_dir}"
  tar -xf source/llama-b11223.tar.gz -C "${source_dir}" --strip-components=1
fi
if ! rg -q tensor_traits_vnni "${source_dir}/ggml/src/ggml-cpu/repack.cpp"; then
  patch -d "${source_dir}" -p1 < patches/vnni-pr-30036.diff
fi
if ! rg -q GGML_QWEN_RANK_ONLY "${source_dir}/src/models/qwen3.cpp"; then
  patch -d "${source_dir}" -p1 < patches/qwen-rank-only-experiment.patch
fi
.venv-build/bin/cmake -S "${source_dir}" -B build-vnni \
  -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DGGML_CUDA=OFF -DGGML_BLAS=OFF \
  -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
.venv-build/bin/cmake --build build-vnni --target llama-server -j 16
