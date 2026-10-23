# 图片中枢与对话工具桥

`server.py` 在本机 8092 管理异步图片任务、生成结果和任务保留策略。`agent_server.py` 在 8094 提供对话工具桥与统一工具协议，使用现有网关完成权限、配额、用量及计费检查。

图片中枢实际配置来自 `deployment/private/config/operator.env`；stdio 图片客户端可读取 `deployment/private/config/image-hub-agent.env`。业务任务与生成结果保留在 `data/image-hub/`。任务处理中发生重启时不会自动再次提交可能已经计费的生成操作。

识图和搜索实现放在 `internal_tools/`。识图允许 64 个真实工具调用，使用进程共享的 16 个执行槽和有界等待。HTTP、SSE、MCP 和通用工具入口共用相同的完成与计费语义。

网络订阅管理、出口验证和切换也在本包中：`egress_subscriptions.py` 合并私有订阅并保留最后有效配置，`proxy_qualifier.py` 验证候选及现有备用出口，`egress_watchdog.py` 处理网络故障，`egress_control.py` 串行化各切换器。参见 [网络切换说明](EGRESS_WATCHDOG.md)。

维护、真实并发验收、保留策略和加密备份统一通过 `ops/` 执行。配置示例与部署方法见 [运维文档](../deployment/README.md)。本目录只保留当前运行模块和必要的回归测试，不保留旧接入记录或测试输出。
