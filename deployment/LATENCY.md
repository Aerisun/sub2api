# 网站与模型响应延迟

模型 ID、供应商映射、价格、思考档位及用户提交的参数保持原值。优化针对页面加载、站内转发、连接生命周期及数据库落盘；已下架的 Step、GLM Flash 不恢复。

## 网站发布

线上入口由 `deployment/site.json` 的 `public_url` 定义。Vite 的资源路径、Vue 路由前缀、API 地址必须使用同一挂载路径。普通根路径构建仍可用于其他部署；不能把根路径产物直接发布到 `/sub2api/`。

```bash
export PYTHONPATH="$PWD/runtime/python-site:$PWD"
python3 -m ops.web build
python3 -m ops.checks.site_latency --authenticated --samples 5 --output var/reports/site-新的时间.json
```

`ops.web build` 使用项目内的 Node/pnpm，缺少构建依赖时按锁文件安装。先构建对话界面，再构建 Vue 界面；发布前解析两个入口的脚本和样式 URL，拒绝越过网站前缀、文件缺失或空文件的产物。所有资源先发布，最后原子替换 HTML；保留旧标签页仍需的内容哈希文件。构建产物位于 `backend/internal/web/dist`，线上静态目录位于 `var/www`。

JS/CSS 随发布生成 gzip 文件，Nginx 的 `gzip_static on` 使用这些文件；无 gzip 的客户端仍取原文件。HTML 重新验证，内容哈希资源长期缓存。Nginx 配置更新先用 `nginx -t` 检查，再平滑 reload，保留已有流式连接。

站内语言包、页面和公开设置并行加载；登录页复用公开设置请求。邻近管理页面在首次渲染后再预加载，隐藏页面及省流量网络不做预加载。聊天输入界面先加载，Markdown 引擎延后加载；下载失败仍显示原始真实文本，代码、数学和图表插件继续按实际内容加载。

## 聊天与 API

真实文本、思考和工具增量直接唤醒 SSE 读取器，不等待数据库检查点。检查点仍以约 300 毫秒为间隔提交，由单个后台写入器顺序处理；数据库变慢时只保留最新待写快照，避免把每次写入和不断增长的回复积压到模型读取路径。每次写入限时 10 秒，失败进入真实错误处理。

停止或完成时先取消并等待检查点写入器，再串行保存完整状态。原有账号归属、Unicode 重连位置、停止生成、费用和最终对话校验继续有效。

网页聊天到本机服务的 HTTP 客户端使用连接池，建连最多 3 秒，空闲连接保留 60 秒；原有 20 分钟整体生成期限保持原值。SSE 在 HTTP EOF 前结束时，先取消请求再关闭响应，避免残留连接拖住后续调用。即使携带持久对话编号，转发层也不自动重放可能已计费的 POST。

公网 Caddy 的 POST 重试限制、识图 HTTP/1.1 连接关闭兼容设置、双入口和既有出口验证/恢复保持有效。识图全局接收 64、上游执行 16，允许原有短暂排队。

## 持续检查

`sub2api-latency-monitor.timer` 每次完成后约 60 秒再检查，带最多 5 秒抖动。检查包括本机网关、Nginx 和公网的健康、公开设置、初始化状态，以及两个网页入口的资源路径和实际资源类型。HTTP 200 的 SPA 回退页不能冒充 JS，健康接口正常也不能掩盖脚本 404。

本机接口超过 250 毫秒或公网接口超过 2 秒记录延迟异常；连续三次失败进入异常状态。状态变化和持续异常写入 journal，最新明细在私有的 `var/reports/latency-current.json`。巡检不重启网关、不修改模型、不创建付费推理。

巡检同时汇总最近 15 分钟已有的模型用量记录：输入不超过 4096 tokens、至少 3 次调用时计算网关首事件 p50/p95。p95 超过 10 秒标记需要检查的模型，查询最多 2 秒，不阻塞业务数据库。该值用于发现后续变慢；首事件不一定含真实文本，真实内容延迟仍以专门验收为准。识图排队和长上下文不混进这项短请求指标。

```bash
python3 -m ops.install
systemctl --user enable --now sub2api-latency-monitor.timer
systemctl --user status sub2api-latency-monitor.timer
journalctl --user -u sub2api-latency-monitor.service -n 20
```

模型的上游网络由既有出口守护负责验证和恢复。真实模型延迟另行验收，不能用健康响应、SSE 心跳或占位内容代替：

```bash
python3 -m ops.checks.public_model_latency --tools --output var/reports/models-新的时间.json
python3 -m ops.checks.image_recognition_concurrency --concurrency 64 --waves 1 --case ocr --output var/reports/recognition-新的时间.json
```

上述两个命令会产生真实付费调用。报告分别记录响应头、首个真实思考/文本、首段正文、完成、用量和错误；短提示验收使用调用方显式的低思考档位，不写入模型配置。成功和达到延迟目标是两个独立结果，退出码非零时应阅读具体字段。

## 网关更新与恢复

后端更新先完成单元测试和聊天并发竞争检查，再保存候选来源与 SHA-256。当前工作区有其他功能修改时必须保留已经上线的功能，且不能把其他任务的改动误提交进延迟修复。

替换网关前暂停识图探针，等待全部账号调用、供应商池任务、图片任务和数据库中的持久聊天任务结束；不能只看一个 HTTP 请求是否结束，也不能排除独立向量调用。持有识图接收槽并再次检查安静窗口后才替换程序和重启。新程序未就绪时恢复原程序，重新检查健康；操作结束后恢复探针。

构建和运行来源、真实请求验收放入 `var/reports`，不上传 GitHub。代码、模板和文档提交后，按 [BACKUP.md](BACKUP.md) 创建新完整快照，实际恢复数据库并回读缤纷云对象后才轮换旧本地备份。

新增网站入口、公开路径或代理层时，同时扩展入口资源检查、真实 API 检查、CI 中的子路径构建和备份清单；不要只增加一个返回 200 的健康页。
