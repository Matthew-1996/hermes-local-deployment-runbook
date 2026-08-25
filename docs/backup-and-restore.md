# Backup and Restore

## 备份目标

每周备份用于在 VM 损坏、重装 Hermes 或切换主机后恢复连续性，而不是复制整个运行环境。

包含：

- `.env`、`config.yaml`、`auth.json`
- `SOUL.md`、`MEMORY.md`、`USER.md`
- Skills、会话、Cron、配对和平台状态
- `state.db`、`kanban.db`、`cron/executions.db` 的 SQLite 在线一致性快照

排除：

- Hermes 源码、Python 虚拟环境
- Node.js 和 Playwright 浏览器缓存
- 本地 STT 模型
- 日志、临时文件和其他可重新下载内容

## 备份链路

Ubuntu 使用 [`scripts/guest/hermes-core-backup.sh`](../scripts/guest/hermes-core-backup.sh)：

1. 在私有临时目录复制核心文件。
2. 使用 SQLite Online Backup API 生成一致性数据库副本。
3. 生成压缩包并在本地验证可读取。
4. 通过 SSH 把标准输入传给 ECS 的强制接收程序。

ECS 使用 [`scripts/cloud/hermes-backup-receive.sh`](../scripts/cloud/hermes-backup-receive.sh)：

1. 写入 `.incoming-*` 临时文件。
2. 验证文件非空且 `tar -tzf` 成功。
3. 原子移动为正式备份名。
4. 生成 SHA-256 校验文件并设置 `0600` 权限。
5. 新备份成功后，才按空间规则清理旧备份。

## 动态保留规则

- 可用空间 `>= 10 GiB`：保留最新 2 份。
- 可用空间 `< 10 GiB`：保留最新 1 份。
- 只匹配专用目录内的 `hermes-local-*.tar.gz` 及其 `.sha256`。
- 新备份失败时不清理旧备份。

## 手动验证

Ubuntu：

```bash
timeout 600 "$HOME/.local/bin/hermes-weekly-backup"
```

ECS：

```bash
cd /home/<BACKUP_USER>/backups
sha256sum -c ./*.sha256
latest=$(ls -1t hermes-local-*.tar.gz | head -n 1)
tar -tzf "$latest" | sed -n '1,40p'
```

## 恢复流程

1. 停止目标主机的 Hermes Gateway。
2. 安装与备份相同或兼容版本的 Hermes。
3. 校验归档 SHA-256 并在临时目录解压。
4. 先备份目标主机当前 `~/.hermes` 核心数据。
5. 覆盖配置、人格、记忆、Skills、会话、Cron 和数据库。
6. 修正所有权和权限：目录通常 `0700`，敏感文件 `0600`。
7. 运行 `hermes config check`、`hermes doctor` 和模型 CLI 测试。
8. 只启动一个 Gateway，依次测试微信和飞书。

恢复时不要盲目覆盖 Hermes 程序目录、浏览器缓存或模型文件。

## 凭证风险

备份包含平台凭证和 API 配置。当前链路通过 SSH 加密传输，归档在 ECS 上依靠专用账号与 `0600` 权限保护，但归档本身没有二次加密。需要更强保护时，可在发送前用 `age` 或等效工具加密，并把解密密钥放在 ECS 之外。

