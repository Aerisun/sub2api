# 缤纷云备份与恢复

备份使用系统设置中已经配置的缤纷云 S3，配置来源为数据库 `settings.backup_s3_config`。不更换桶、访问身份或现有数据库定时备份策略，不把备份或实际配置上传 GitHub。

## 两类恢复资料

| 类型 | 范围 | 存储与恢复入口 |
| --- | --- | --- |
| 系统数据库备份 | PostgreSQL 业务数据和系统设置 | 保持系统备份页面及原有定时任务；只恢复数据库 |
| 完整项目快照 | 源码、配置、运行资源、数据库、VPN 身份、外置续期组件 | age 加密文件；通过运维命令或缤纷云控制台取得，按本文恢复 |

完整项目包不能作为 SQL 文件交给数据库恢复页面。两个目录分开，系统数据库备份的过期清理不处理项目完整包。

## 权威清单与包内结构

`deployment/backup-layout.json` 是项目备份范围的唯一配置。`ops.backup` 按该清单打包，并把源码成员与必要文件写入 `manifest.json`；验证时检查它们没有遗漏。

```text
manifest.json                     时间、提交、范围、成员及组件校验
database.dump                     PostgreSQL 一致性逻辑快照
redis.rdb                         Redis 快照
project/                          当前 Sub2API 项目
  deployment/private/             实际配置、SSH 和恢复用 S3 凭据
  secrets/                        供应商身份和网络订阅，排除 age 解密密钥
  data/                           业务状态，SQLite 用在线快照替换原文件
  bin/ runtime/ runtime-redis/    当前运行程序与依赖
  var/www/ var/reports/           静态资源与验收记录
external/auto-get-api/            保留在外部的自动续期项目及一致性账本
host/systemd/auto-get-api.service 外部续期服务的实际定义
host/tailscale/tailscaled.state   主机 VPN 身份恢复资料
```

日志、临时上传、依赖构建缓存、旧备份、活动 socket、SQLite WAL/SHM 和活动 PostgreSQL 数据目录不打包。公开 CPU 模型权重通过项目的固定下载脚本和 SHA-256 恢复；其引擎程序、构建补丁、私有配置已经备份。

退役的外部 `network` 与 `Gemini-API` 保留一份删除前的加密归档，归类为 `retired`，用于查阅历史配置；日常恢复不需要解开或启动这些旧项目。

## 创建与上传

在项目根目录执行：

```bash
export PYTHONPATH="$PWD/runtime/python-site:$PWD"
python3 -m ops.backup create --publish
```

该命令读取系统已有的 S3 配置，在私有目录保存恢复副本；创建加密快照后，在临时 PostgreSQL 中实际恢复，检查 SQLite 和源码覆盖，再上传。上传完成后重新下载云端对象的每一个字节计算 SHA-256；通过验证才记录成功回执。

只做本地验证时可用 `python3 -m ops.backup create`，随后执行：

```bash
python3 -m ops.backup verify backups/具体文件.tar.gz.age --restore-database
python3 -m ops.backup_store backups/具体文件.tar.gz.age
```

云端对象位于系统配置前缀下：

```text
<prefix>/project-snapshots/current/<archive>.tar.gz.age
<prefix>/project-snapshots/current/<archive>.tar.gz.age.sha256
<prefix>/project-snapshots/current/<archive>.tar.gz.age.verification.json
<prefix>/project-snapshots/retired/<retired-archive>...
```

本地 `<archive>.age.cloud.json` 记录实际桶、对象键、时间、长度和校验结果。文件仍受私有目录权限保护。项目快照目前由运维命令触发，不另设定时器，也不自动清理云端历史；轮换时至少保留一份实际恢复验证成功的完整快照。原有数据库定时任务保持系统中的设置。

## 密钥与凭据

`secrets/backup/identity.agekey` 是独立解密密钥，权限 0600，绝不放入它所解密的包，也不上传 GitHub 或备份桶。应另存到操作者控制的安全位置；丢失密钥后无法解密。包内 `deployment/private/config/backup-store.json` 是系统 S3 配置的恢复副本，实际值只在加密文件中进入云存储。

整机丢失时先通过缤纷云控制台下载对象，或使用操作者另存的 S3 凭据下载。不要依赖尚未恢复的数据库来获取下载凭据。

## 恢复顺序

1. 停止目标主机上会修改同一份业务数据的服务。下载快照与 SHA-256，校验下载文件，再在私有空目录解密：`age -d -i identity.agekey -o snapshot.tar.gz 备份.tar.gz.age`。
2. 用 `tar --no-same-owner -xzf snapshot.tar.gz` 解包。将 `project/` 放为新的 Sub2API 根目录，`external/auto-get-api/` 放到它的同级。不要将历史包覆盖仍在线运行的项目。
3. 将另存的解密密钥装回 `secrets/backup/identity.agekey`，保持权限 0600。按新主机 Python 版本安装 `requirements-aerisun.txt` 到 `runtime/python-site`。运行 `python3 -m ops.install` 渲染新路径；这一步只注册定义，不启动服务。
4. 从私有 `runtime.env` 取得数据库身份，初始化空 PostgreSQL，通过 `pg_restore --no-owner --no-privileges --exit-on-error` 恢复 `database.dump`。Redis 停止时，把 `redis.rdb` 放入其私有配置指定的数据目录。SQLite 已是完整一致性文件，不合并旧 WAL。
5. 处理共享 VPN：原身份仍在运行的主机不要覆盖其状态；新增主机应单独注册身份。如果确需恢复包内 VPN 身份，先确认所有使用该状态的旧守护进程已经停止，再安装 `host/tailscale/tailscaled.state` 并恢复权限。验证 `python3 -m ops.tailnet --check`。
6. 检查外置 `auto-get-api` 的账户、兑换库存和账本；按新路径调整 `host/systemd/auto-get-api.service` 后注册。确认同一账户没有另一个自动续期进程，再启动。
7. 依次启动数据库、Redis、出口、远端推理连接、供应商池、工具、网关、Web 和隧道。CPU 推理端恢复按 `inference/README.md` 操作，保留原模型和量化规格。
8. 检查 `python3 -m ops.services status`、网络守护、订阅与监控定时器，再做小规模真实调用。恢复验证与云端验证回执放入 `var/reports/`，完成后清理临时解密目录。

新增功能的配置与备份登记规则见 [CONFIGURATION.md](CONFIGURATION.md)。
