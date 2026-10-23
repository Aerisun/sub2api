# Qwen3 Embedding CPU 服务

此目录收录当前运行的调度器、配额入口、启动脚本和回归测试。入口分别为 `scripts/embedding_pool.py` 和 `scripts/quota_gateway.py`，工作进程由 `scripts/run_worker.sh` 管理。

实际环境、弹性调度配置、鉴权文件和配额库集中备份到 `deployment/private/inference/embedding/`。原硬件的 CPU 掩码、并行数、模型别名及输出维度保持原值。当前精确引擎及共享库位于加密快照的 `runtime/inference/embedding/bin/`。

恢复公开的 Qwen3-Embedding-4B Q8_0 权重时，使用 `scripts/bootstrap.sh` 中固定的地址和 SHA-256；不要改用其他量化规格。引擎恢复优先使用快照中的当前程序，bootstrap 中的通用发行版用于初始获取，不能直接覆盖已经验证的 CPU 构建。

当前部署入口、回归命令和恢复关系见 [CPU 推理总览](../README.md) 与 [统一运维文档](../../deployment/README.md)。
