# 出口验证与故障切换

实际配置位于 `deployment/private/config/`，三个 Mihomo 工作目录位于 `deployment/private/egress/`。配置中的供应商认证和订阅地址只随加密备份保存。

订阅刷新失败、内容无效或没有合格节点时，保留上一份可用结果。候选需要通过真实模型生成、地区和网络检查。现有节点与备用节点持续检查；即使订阅没有变化，也会定期重新验证。健康的主出口在没有明显收益时保持稳定。

更新代理池先保留原出口、选中新出口并确认成功，再清理旧成员。所有控制器共用同一把切换锁，切换前重新读取当前选择，过期检查不能覆盖较新的选择。验证失败会保留或恢复原配置。

日常切换仅操作已验证的代理组，不重载整个配置、不重启进程，也不重放已输出内容的请求。完整配置重载的隔离测试已证明会关闭已有连接，维护时必须使用排空流程。

服务定义保存在 `systemd/` 模板，使用 `python3 -m ops.install` 注册。状态文件保存在 `data/`，验证结果写入 `var/reports/`。入口包括 `sub2api-egress-subscriptions.timer`、`sub2api-proxy-qualifier.service`、`sub2api-egress-watchdog.service` 和独立 NVIDIA 验证器。
