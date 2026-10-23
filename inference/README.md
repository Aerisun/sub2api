# CPU 向量推理组件

`embedding/` 与 `reranker/` 收录当前远端部署的应用代码。embedding 的入口为 `scripts/embedding_pool.py`，配额入口为 `scripts/quota_gateway.py`；reranker 的入口为 `scripts/pool.py`。二者都保留原来的模型名称、量化规格、排队、缓存和 CPU 预算。

实际配置和原始服务定义放在 `deployment/private/inference/`，随加密快照保存。当前实际执行的 llama-server 及其共享库备份到 `runtime/inference/{embedding,reranker}/bin/`；CPU 补丁放在各组件的 `patches/`。这些只是收录和备份，不改变远端正在处理的请求。

线上模型为 Qwen3-Embedding-4B Q8_0 和 Qwen3-Reranker-4B Q8_0。下载脚本和构建脚本固定校验和；恢复时必须使用相同文件，不能用另一个量化文件代替。模型权重按下载脚本恢复，公开权重不作为 Git 文件或加密备份的重复大文件上传。

应用回归检查：

```bash
PYTHONPATH=runtime/python-site:inference/embedding/scripts python3 -m unittest discover -s inference/embedding/tests
PYTHONPATH=runtime/python-site:inference/reranker/scripts python3 -m unittest discover -s inference/reranker/tests
```

远端服务实际安装路径和硬件配额以私有快照为准。恢复到另一目录时先调整文件路径、注册对应服务并验证健康，再切换 SSH 转发入口；调用模型、输出维度和计费配置保持原值。
