# NVIDIA 转发池

认证入口为本机 8096，使用独立的 17896 出口。当前模型、队列、超时和调度限制的唯一配置是 `deployment/private/config/nvidia-pool.json`；供应商密钥在 `secrets/nvidia/`。密钥不进入日志、源码或健康响应。

池保留会话到密钥的亲和关系、跨模型滚动配额、密钥鉴权状态和真实上游缓存用量。扩充密钥时只允许追加原列表，SIGHUP 不打断当前租约。已输出语义内容的推理不会重放，失败识图不收图片费用。

文本仍按原有账户和模型映射调度。私有识图使用已部署的 `nvidia/nemotron-3.5-super-vl-120b-a12b` 与 `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`；它们不作为新增公开模型发布。GLM Flash 和 Step 已按要求下架，不能由定期路由同步重新加入。

出口通过真实文本和图片生成验证后才进入线上池。`egress_transport.py` 为新出口创建连接池，旧响应自然排空；`server.py` 按出口切换批次隔离临时故障。旧出口的迟到失败不能影响新出口，真实生成成功的证明只解除对应模型的临时熔断，不清空密钥或供应商配额。

服务为 `sub2api-nvidia-pool.service`，网络验证器为 `sub2api-nvidia-egress-qualifier.service`。当前账户可用路由由 `python3 -m ops.nvidia_routes` 维护，完整部署与备份说明见 [运维文档](../deployment/README.md)。回归测试与真实验收工具分别保留在本组件及 `ops/checks/` 中，结果写入 `var/reports/`。
