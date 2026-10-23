# Qwen3 Reranker CPU 服务

此目录收录当前 `scripts/pool.py` 调度器、`scripts/result_cache.py`、启动脚本及回归测试。维持原有排队、结果缓存、扇出、CPU 预算和返回结果顺序，模型保持 Qwen3-Reranker-4B Q8_0。

实际环境、弹性配置、服务定义与鉴权材料集中备份到 `deployment/private/inference/reranker/`。当前精确引擎及共享库在加密快照的 `runtime/inference/reranker/bin/`。模型下载参数在 `model.json`，`scripts/download_model.py` 逐块下载并验证 SHA-256。

当前 CPU 构建补丁位于 `patches/`，固定的 llama.cpp 原始源码包随快照放在 `runtime/inference/sources/`。重新构建前将该源码包放到本组件的 `source/llama-b11223.tar.gz`，运行 `scripts/build_cpu_vnni.sh`；脚本验证源码、补丁与模型的固定哈希。它不自动切换线上程序。

完整部署与恢复说明见 [CPU 推理总览](../README.md) 与 [统一运维文档](../../deployment/README.md)。
