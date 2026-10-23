# 统一工具

当前工具为 `image_generation`、`image_recognition` 和 `web_search`。REST、流式响应、通用工具调用及 MCP 共用实现，继续使用原网关的鉴权、模型权限、用量及价格。

识图接受 64 个并发调用，内部执行上限为 16，其余调用在有界队列中等待。各工作进程通过文件锁和 SQLite 共享槽位、路由健康和租约。最多只恢复未产生可见语义且明确未收图片费用的失败；已开始输出的模型调用不会重放。成功返回要求完整终止与真实 usage，只有完成的识图收取图片费用。

`vision_routing.py` 维护现有的两个私有 NVIDIA 识图模型；它们不加入客户公开模型列表。`vision_monitor.py` 进行真实内容检查并恢复健康路由，业务图片和客户提示词不写入路由状态库。主模型、推理参数和价格保持现有配置。

真实验收使用 `python3 -m ops.checks.image_recognition_concurrency`，同时校验内容、完成状态、并发和计费；测试文件保留在本组件，输出进入 `var/reports/`。维护与备份参见 [部署说明](../deployment/README.md)。
