# Operations Runbook

## 日常健康检查

### ECS 中转机

```bash
systemctl is-active sshd-tunnel.service
ss -lnt 'sport = :<DEDICATED_SSH_PORT> or sport = :<ECS_LOOPBACK_PORT>'
df -h /home/hermesbackup/backups
```

预期：专用 SSH 服务 active，公网专用端口和 ECS 回环转发端口监听正常。

### macOS 宿主机

```bash
pmset -g batt
pmset -g | grep -Ei 'SleepDisabled|sleep'
launchctl print system/com.example.hermes-reverse-tunnel
launchctl print system/com.example.hermes-ubuntu-vm
/Applications/VirtualBox.app/Contents/MacOS/VBoxManage \
  showvminfo hermes-ubuntu --machinereadable | grep '^VMState='
```

预期：使用 AC Power、`SleepDisabled 1`、隧道与 VM LaunchDaemon 正常、VM 为 running。

### Ubuntu 虚拟机

```bash
systemctl is-enabled hermes-gateway.service
systemctl is-active hermes-gateway.service
systemctl is-active mihomo.service
hermes status
hermes config get model
hermes fallback list
free -h
swapon --show
df -h /
systemctl list-timers hermes-weekly-backup.timer --all --no-pager
```

预期：Gateway 与 Mihomo enabled/active，消息平台 configured，主模型与备用链正确，内存和磁盘仍有余量，备份 Timer 有下次运行时间。

### 模型与代理快速验收

代理端点探测不携带业务凭据，不会发起模型推理：

```bash
curl -4 -sS -o /dev/null \
  --proxy http://127.0.0.1:<GUEST_PROXY_PORT> \
  --connect-timeout 10 --max-time 30 \
  -w 'HTTP=%{http_code} CONNECT=%{time_connect}s TLS=%{time_appconnect}s FIRST_BYTE=%{time_starttransfer}s TOTAL=%{time_total}s\n' \
  https://chatgpt.com/backend-api/codex
```

未携带凭据时 `HTTP=403` 表示网络、代理和 TLS 已到达 Codex 端点。`HTTP=000`、curl 退出码 `28` 或 `35` 才表示连接或 TLS 故障。

真实主模型验收：

```bash
HTTP_PROXY=http://127.0.0.1:<MIHOMO_PORT> \
HTTPS_PROXY=http://127.0.0.1:<MIHOMO_PORT> \
NO_PROXY=127.0.0.1,localhost \
timeout 180 hermes -z "仅回复：主模型调用成功"
```

完成配置后还必须在至少一个生产消息渠道发送测试消息。CLI 成功不能替代 Gateway 和渠道验收。

## 远程登录

从 ECS 登录 Mac：

```bash
ssh -p <ECS_LOOPBACK_PORT> <MAC_USER>@127.0.0.1
```

从 Mac 登录 Ubuntu VM：

```bash
ssh -p <VM_NAT_SSH_PORT> <VM_USER>@127.0.0.1
```

不要把密码写进命令、脚本或 Shell 历史。密码认证场景由操作者在提示符中手工输入。

## 临时使用旧 Mac

需要释放约 5GB 内存时，可以优雅关闭 VM：

```bash
/Applications/VirtualBox.app/Contents/MacOS/VBoxManage \
  controlvm hermes-ubuntu acpipowerbutton
```

确认：

```bash
/Applications/VirtualBox.app/Contents/MacOS/VBoxManage \
  showvminfo hermes-ubuntu --machinereadable | grep '^VMState='
```

VM 关闭期间，微信和飞书机器人不可用。恢复时重新启动 VM，随后检查 Ubuntu Gateway。

## 故障定位顺序

1. **渠道层**：只有微信或飞书异常，检查对应适配器日志和平台连接。
2. **Hermes 层**：两个渠道都异常，检查 Ubuntu Gateway、模型 API 和 DNS。
3. **代理层**：Codex 单独异常，分别检查 Mihomo、Guest 代理端口、Mac 代理隧道和 Mac 客户端。
4. **VM 层**：Ubuntu SSH 不通，检查 VirtualBox 状态和 NAT 规则。
5. **Mac 层**：ECS 回环端口不监听，检查 Mac 电源、网络和反向隧道 LaunchDaemon。
6. **ECS 层**：Workbench 或专用 SSH 端口不通，检查 ECS 服务、安全组和磁盘。

## 故障切回云端

仅在本地节点无法及时恢复时使用：

1. 确认本地 Hermes Gateway 已停止或本地 VM 已关机。
2. 在云端恢复最新备份并检查配置版本。
3. 启动云端 Gateway。
4. 分别测试微信和飞书。
5. 恢复本地后，先停云端 Gateway，再启本地 Gateway。

禁止在未确认本地停止时直接启动云端 Gateway。

## 日志边界

排障命令应优先过滤并脱敏：

- 不输出 `.env`、`auth.json` 或私钥内容
- 不粘贴 WebSocket URL 中的 access key 或 ticket
- 用户 ID、群 ID、IP 和主机指纹只在必要的私密运维环境中查看
- 分享日志前先替换 Token、Authorization Header 与账号标识
