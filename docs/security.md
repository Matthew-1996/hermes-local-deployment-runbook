# Security Boundaries

## 必须遵守

- 不把 `.env`、`auth.json`、私钥或真实备份归档提交到 Git。
- 不在命令行参数中传递密码、Token 或 API Key。
- 不公开 ECS 公网 IP、实例 ID、用户名、平台用户 ID、群 ID 或主机指纹。
- 不直接把 Mac SSH、Ubuntu SSH 或 Hermes 管理端口暴露到公网。
- 不同时运行本地和云端两个 Hermes Gateway。
- 不允许备份账号获取 Shell、TTY、密码登录、Agent 转发或 TCP 转发。
- 不在新备份失败时删除旧备份。

## SSH 角色分离

建议至少分为两个独立账号或密钥：

1. **反向隧道账号**：只允许指定回环端口的 remote forwarding，不能执行命令。
2. **备份账号**：只允许强制执行备份接收程序，不能端口转发。

迁移期间使用的临时密钥必须在验收完成后同时从授权端和持有端删除。

## GitHub 发布前扫描

```bash
git grep -nEI '([0-9]{1,3}\.){3}[0-9]{1,3}|sk-[A-Za-z0-9_-]+|BEGIN (OPENSSH|RSA|EC) PRIVATE KEY|api[_-]?key|app[_-]?secret|access[_-]?token|password[[:space:]]*=' -- . \
  ':!docs/security.md'
```

还应检查：

- `.DS_Store`、终端截图、日志与命令历史
- `*.pem`、`*.key`、`id_*`、`authorized_keys`
- 备份压缩包、SQLite 数据库和 `.env*`
- 文档中复制的真实路径、IP、指纹和账号 ID

## GitHub 仓库建议

- 首次发布使用 private visibility。
- 启用 Secret Scanning 和 Push Protection（如账户方案支持）。
- 提交只包含模板与文档，不包含机器导出的真实配置。
- 需要分享时先审阅历史提交，而不只是当前工作树。

