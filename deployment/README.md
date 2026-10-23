# Aerisun Sub2API 运维

这是当前线上服务的完整项目，基于 `Wei-Shaw/sub2api`，在 `Aerisun/sub2api` 的 `aerisun` 分支维护。主机上只保留这一份源码与部署目录。模型 ID、推理参数、价格及文本供应商映射沿用线上配置；已按要求下架的 Step 和 GLM Flash 不再发布。

## 目录

| 路径 | 用途 |
| --- | --- |
| `backend/`、`frontend/`、`chat-ui/` | 网关、Vue 管理界面和 React 对话界面源码 |
| `image_hub/`、`internal_tools/` | 图片生成、识图、MCP、搜索及流式工具 |
| `nvidia_pool/`、`qiniu_pool/` | 原有供应商的密钥调度、配额与连接管理 |
| `inference/` | CPU embedding/reranker 的运行脚本、构建补丁和必要回归测试 |
| `ops/` | 唯一运维入口；实际付费验收工具放在 `ops/checks/` |
| `systemd/`、`deployment/examples/` | 可提交的服务模板和无密钥配置示例 |
| `deployment/private/`、`secrets/` | 实际配置、订阅、供应商凭据和 SSH 密钥，不提交 Git |
| `deployment/generated/` | 按项目实际路径渲染的 systemd/logrotate 配置 |
| `data/`、`postgres_data/`、`redis_data/` | 服务状态和业务数据，不提交 Git |
| `var/www/`、`var/log/`、`var/reports/` | 当前静态资源、日志和验收记录 |
| `bin/`、`runtime/`、`runtime-redis/` | 当前运行程序与依赖，不保留旧候选构建 |
| `backups/` | 加密快照、校验和与恢复验证结果 |

`~/.config/systemd/user/` 只注册指向项目内生成文件的链接，不保留第二份配置。系统提供的 SSH、systemd 和共享 Tailscale 守护进程属于主机基础设施。

## 常用操作

在项目根目录执行：

```bash
export PYTHONPATH="$PWD/runtime/python-site:$PWD"
export PATH="$PWD/runtime/bin:$PWD/runtime/go/bin:$PWD/bin:$PATH"
python3 -m ops.services status
python3 -m ops.install
```

`ops.install` 渲染并注册服务，不重启服务。它根据 `deployment/site.json` 的原安装路径调整恢复后的私有配置路径。

需要更新工具或供应商转发服务时：

```bash
python3 -m ops.services restart sub2api-nvidia-pool sub2api-agent-images
```

该入口等已接收的调用结束后才重启，并暂停识图探针。独立运行的 embedding/reranker 长批次不会阻塞无关服务的维护。到期仍未排空时保留原服务。完整 Mihomo 配置重载会中断连接，必须先排空；日常节点切换仅使用已验证的代理组选择，不重载整个代理进程。

## 网络恢复

两份订阅在私有目录中合并，更新失败或候选为空时保留上一份有效配置。候选节点通过实际模型生成、地区和网络检查后才可进入线上池。当前出口和备用出口定期重新检查，订阅内容不变时也会重新验证。所有切换器共用锁，并检查选择是否生效。

NVIDIA 的 HTTP 连接池和熔断统计按出口切换批次隔离：旧响应继续结束，新请求使用新连接；旧出口的迟到错误不会熔断新出口。真实生成验证可以解除新出口对应模型的临时熔断，供应商配额和密钥鉴权限制保持有效。已输出内容的推理请求不自动重放。

公网入口的 Caddy 配置在 `tunnel/` 中。保留识图工具 HTTP/1.1 响应的连接关闭设置，以及公网和 Tailscale 两条入口。禁止给已提交的推理 POST 增加入口自动重放。

## 验收与构建

```bash
python3 -m ops.checks.public_model_latency --tools --output var/reports/public-models.json
python3 -m ops.checks.image_recognition_concurrency --concurrency 64 --waves 2 --case ocr --output var/reports/recognition64.json
```

这些是明确的真实付费调用，临时测试用户和密钥会在结束后删除。识图检查同时覆盖 REST、SSE、MCP 和通用工具调用，逐个验证图片内容、完成状态、用量和扣费。内部允许短暂排队，接收上限 64，供应商执行上限 16。

离线回归测试在各组件中维护；不保留根目录的一次性测试脚本。后端使用 Go 1.27，前端使用 Node 24 和 pnpm 9。先安装 `chat-ui/` 与 `frontend/` 中锁定的依赖，再执行 `make build-frontend`。Docker 构建也会从源码编译独立对话界面；生成资源不提交 Git。

## 加密备份与恢复

```bash
python3 -m ops.backup create
python3 -m ops.backup verify backups/具体备份.tar.gz.age --restore-database
```

备份包含当前代码与 Git 历史、实际私有配置、供应商与隧道凭据、运行程序和依赖、当前静态资源、PostgreSQL 逻辑快照、Redis RDB，以及通过 SQLite 在线备份接口取得的业务库。它不打包正在变化的 PostgreSQL 数据目录，也不复制旧构建、旧备份和临时上传缓存。

验证会解密检查组件校验和，在只开放私有 Unix socket 的临时 PostgreSQL 实例中实际恢复，并检查 SQLite 完整性。验证完成后临时明文目录自动删除。

解密文件是 `secrets/backup/identity.agekey`，权限为 0600。它必须另存一份；不会提交 Git，也不会随加密文件上传。GitHub Release 只上传 `.age` 文件、SHA-256 和不含凭据的验证结果。

灾难恢复步骤：

1. 在空目录解密、解包快照，保留 `database.dump`、`redis.rdb` 和 `manifest.json`；将 `project/` 作为新的 `sub2api` 项目目录。先校验下载文件的 SHA-256。
2. 使用另存的解密文件解密：`age -d -i identity.agekey -o snapshot.tar.gz 备份.tar.gz.age`。只处理自己的、已验证的快照。
3. 运行 `python3 -m ops.install`，重新生成当前路径对应的服务配置。由私有 `runtime.env` 中的用户和密码初始化空 PostgreSQL，使用 `pg_restore --no-owner --no-privileges --exit-on-error` 恢复业务库。
4. Redis 停止时，将 `redis.rdb` 安装到其私有配置指定的数据目录；保留原有认证与配额状态。
5. 先启动数据库、Redis、三个出口及推理连接，再启动池、图片工具、网关、Nginx、隧道和监控定时器。先检查 `ops.services status`，再执行小规模真实调用。

CPU 推理目前运行在私有 SSH 配置指定的远端主机，8091/8101 经项目内的连接服务转发。其应用代码、私有配置、鉴权文件、配额数据库、实际引擎程序及构建补丁一并保存。模型权重由 `inference/` 中的下载脚本和 SHA-256 校验恢复，不重复上传数 GB 的公开权重；不会自动更换模型或量化规格。参见 [CPU 推理说明](../inference/README.md)。

## 保持目录干净

只在源码目录维护当前实现。验证结果写入 `var/reports/`，日志写入 `var/log/`，临时构建写入 `var/build/`。旧脚本、浏览器录制、源码副本和失效补丁在新快照恢复验证后删除。运行 `python3 -m ops.maintenance --apply` 执行既有的缓存与日志保留策略。
