# 七牛转发池

本机认证入口为 8095，供应商请求和模型映射沿用当前部署。唯一实际配置为 `deployment/private/config/qiniu-pool.json`，密钥在 `secrets/qiniu/`，状态在配置指定的 `data/` 文件中。模型名称、价格和推理参数由现有网关配置决定。

会话优先使用已有密钥；忙碌、限流或鉴权失败时选择合格的其他密钥。持久化检查点保留真实配额和会话亲和关系，不保存客户提示词或完整回复。提供方的缓存用量按实际返回值传递和计费。

文本、思考、工具调用和 usage 按原顺序增量转发。无事件的成功响应会转成明确错误；已经开始输出的请求不会重放。健康入口只提供编号、计数、并发和冷却信息。

维护入口是 `python3 -m ops.services restart sub2api-qiniu-pool`，会先等待在途请求结束。使用 `python3 -m unittest qiniu_pool.tests qiniu_pool.checkpoint_test qiniu_pool.transport_test` 检查调度、持久化和连接管理。当前部署不再保留一次性接入、迁移和旧价格配置脚本。

参见 [统一运维与加密恢复](../deployment/README.md)。
