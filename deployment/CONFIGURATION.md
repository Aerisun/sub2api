# 配置结构与新增功能约定

Sub2API 的源码、运维入口、网络守护和部署模板统一保存在本项目中。`auto-get-api` 根据要求独立放在项目同级；本次清理不删除或修改这个独立项目。`Gemini-API` 已退役，完整项目恢复不需要启动它。

## 配置分层

| 层级 | 位置 | 内容与管理方式 |
| --- | --- | --- |
| 公开部署目录 | `deployment/site.json` | 组件配置路径、端口、源码分支；不写密码、订阅地址或令牌 |
| 公开备份目录 | `deployment/backup-layout.json` | 打包目录、必要文件、运行程序、主机状态和外部组件的唯一清单 |
| 公开配置示例 | `deployment/examples/` | 字段结构、占位符和可公开默认值；与实际配置分开 |
| 实际组件配置 | `deployment/private/config/` | 环境变量、供应商池、出口调度、网络守护和恢复用 S3 配置；权限 0600 |
| 实际出口配置 | `deployment/private/egress/` | 三套 Mihomo 配置、经验证的代理池和控制器凭据 |
| 身份与订阅 | `deployment/private/ssh/`、`secrets/network/` | SSH 身份、订阅原文、有效缓存和候选池；不复制到公开示例 |
| 动态状态 | `data/` | 工具任务、配额、缓存、SQLite；通过在线快照备份 |
| 生成文件 | `deployment/generated/` | 从 `systemd/` 等模板生成；只注册系统链接，不手工维护第二份 |
| 运行资源 | `bin/`、`runtime/`、`var/www/` | 当前程序、依赖和静态资源；源码、依赖锁文件与其用途配套 |
| 本地备份 | `backups/` | 加密包、校验、恢复证明和缤纷云回执；整个目录不进入 Git |

实际敏感值均不提交 GitHub，包括加密备份和恢复回执。GitHub 仅接收代码、模板和脱敏说明。历史备份附件已经从 Release 移除。

每次创建快照都会刷新本地 `deployment/private/INVENTORY.json`，记录私有文件路径、权限、校验和及归档方式，不记录实际敏感值。新配置按上述分层加入后会自动进入清单与加密备份；解密密钥被明确标记为单独保存。

## 新用户默认分组

本站新注册用户自动加入“VIP 客户”（分组 103）。`deployment/site.json` 的 `signup_customer_group_id` 记录该分组，实际注册配置由 `deployment/private/config/runtime.env` 的 `DEFAULT_USER_ALLOWED_GROUPS=103`、`DEFAULT_USER_RESTRICT_PUBLIC_GROUPS=true`、`DEFAULT_USER_OPENAI_CONCURRENCY=2` 控制。邮箱注册和第三方首次注册均在创建用户的事务中保存分组授权；来源专属赠额和并发设置继续生效。

修改环境变量后，等已接收请求结束再重启网关。默认分组必须保持活跃且未删除，不能指向已退役分组。已有账号的授权通过后台单独调整；商品供应和国产模型分组的设置不得覆盖本站的 VIP 注册默认值。

## 网络组件

`ops/tailnet.py` 与 `systemd/service-tailnet.service` 管理网络恢复守护。实际配置为 `deployment/private/config/tailnet.json`，使用项目内 `bin/tailscaled`。它先接管已有的 VPN 连接；只有不存在同用户守护进程时才尝试用原身份状态恢复，避免重复占用状态。重启守护不会终止它接管的主机 VPN 进程。

共享 VPN 的活动状态和 socket 仍由主机在 `~/.local/share/tailscale/` 管理，避免迁移活动身份导致断网。身份文件被单独收入加密快照的 `host/tailscale/`，socket 不备份。原外部 `network` 文件夹已删除，Sub2API 不再引用它。

`image_hub/egress_ready.py` 默认读取项目私有出口配置；其他出口通过显式 `--config` 指定。不要重新引入外部项目路径。完整 Mihomo 重载会关闭连接，必须使用既有排空入口；日常健康出口切换不做完整重载。

## 新增功能的接入步骤

1. 在现有组件内扩展，或新建一个有明确职责的组件目录。所有 Sub2API 新功能放在项目内；一次性脚本不进入根目录，回归测试放在所属组件内。
2. 为新配置定义字段、类型、默认值、是否敏感及版本号。先校验配置再启动；配置更新写临时文件后原子替换。新增字段保持旧配置兼容，不在加载时静默改写现有值。
3. 将真实配置放入 `deployment/private/config/<component>.json` 或组件专属私有子目录；将无密钥示例放入 `deployment/examples/`。在 `deployment/site.json` 的 `configuration` 中登记组件和路径。避免多个文件重复维护同一凭据。
4. 将可变数据放入 `data/<component>/`，SQLite 使用 `.sqlite3` 后缀，以便现有在线快照发现。Redis 和 PostgreSQL 使用统一服务。其他数据库格式必须先实现一致性备份与恢复验证，再上线。
5. 在 `systemd/` 增加使用 `@ROOT@`、`@HOME@` 的模板，经 `python3 -m ops.install` 注册。该命令不会自动重启服务；更新推理链路时先排空已接收请求。
6. 在 `deployment/backup-layout.json` 的 `required_files` 中加入新组件的关键配置或入口。`deployment/private/`、`data/` 已整体纳入；新增其他目录或程序时同步更新 `project_trees`、`runtime_files`。新外部依赖必须明确路径、所有者与恢复方式，不能靠运行主机上的隐含文件。
7. 更新本说明或组件文档，执行相关测试、秘密扫描和真实恢复验证，再提交代码。业务允许时做小规模真实调用；会产生费用的验证记录写入 `var/reports/`。

现有模型 ID、价格、推理控制和供应商映射按用户要求保留。配置结构整理和新组件接入不能顺带修改这些值。

## 新增模型

新增型号必须先用现有账号、现有出口和当前客户端版本完成真实文字、图片与工具调用验证。旧版客户端可能拿到不完整的模型目录；诊断请求应使用后台已同步的版本，不能据此改动其他模型的路由或参数。

账号的 `credentials.model_mapping` 只增加新型号的同名映射；`extra.upstream_model_metadata.models` 只追加该型号从上游读取的能力。公开分组只追加新型号的 `model_allowlist` 和 `model_pricing`，保留全部既有条目。凭据和账号配置留在数据库及加密备份内。

后台接口中的凭据是脱敏视图，不能直接合并后替换完整 `credentials`。更新前通过 `ops.credentials.accounts` 读取指定账号的完整记录，只调整目标字段，保存后回读核对地址、协议、能力及模型映射；不得把内部池令牌发送到默认官方入口。

代码中的型号识别、计费兜底、网页原生图片入口、思考选项需要同步接入。公开价格与来源记录在 `resources/model-pricing/official-site-prices.json`；通过 `ops.generate_guide`、`ops.generate_prices` 更新共享目录与说明页，再用 `ops.web build` 发布。不能用其他模型替代新型号来通过测试。

`gpt-6.1-terra` 按 [OpenAI 型号说明](https://developers.openai.com/api/docs/models/gpt-6.1-terra) 接入：标准输入/输出为每百万 Token $1/$5，缓存读取/写入为 $0.05/$1.25；本站 Fast ×3、Flex ×0.5。当前 Codex 通道初始上下文 500,000、最大 872,000 Token，优先采用真实通道元数据；网页默认低强度，允许关闭思考和显式选择其他档位。新增型号不改变现有型号的默认值或价格。

上线后从公网核对真实返回型号、首段内容时间、完整结束事件、用量和扣费，并通过网页图片上传验证。确认旧配置逐项未变后提交代码，使用系统缤纷云完成包含数据库的加密快照和恢复校验。真实测试记录保留在忽略上传的 `var/reports/`。

## 已退役的 GLM 通道

`glm-5.2`、`glm-5.3` 及早先退役的 GLM Flash 不再发布。失效专用账号已删除；相关分组的可用型号、独立价格、固定路由以及 NVIDIA 同步映射均已移除。保留历史调用与计费记录，不能把已删除型号映射到其他模型。

旧 GLM 专属思考参数、诊断日志和临时图片托管代理已删除。Nemotron 仍使用的识图能力归入 `openai_auxiliary_vision.go` 和对应缓存组件，配置统一为账号的 `image_bridge_*` 字段，真实视觉模型及参数保持原值。识图工具继续使用原有 64 接入、16 上游执行限制和短暂排队策略。

模型下架也要清理网页共享目录、导出示例、价格来源和发码商品配置，避免定时同步或重新生成配置时恢复旧型号。其他型号的既有价格、额度和参数不随清理调整。

## 外置自动续期

`../auto-get-api` 的源码、实际配置与运行位置保持原样，服务名仍为 `auto-get-api.service`。完整项目备份通过清单只读收录其源码、`.store` 配置、凭据和兑换账本；SQLite 账本使用在线快照，不直接复制 WAL 文件。恢复时仍放在 Sub2API 同级，并检查续期账户只运行一个进程。

备份范围、缤纷云目录和恢复步骤见 [BACKUP.md](BACKUP.md)。
