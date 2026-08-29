# GPT + Hermes Deployment Guide

本指南把旧 Intel Mac、VirtualBox Ubuntu、Hermes、ChatGPT Codex 订阅、Mac 本地代理、消息 Gateway 和云端备份串成一套可重复实施的生产方案。

版本和上游行为会变化。执行前应复核 [Hermes 官方安装文档](https://hermes-agent.nousresearch.com/docs/getting-started/installation) 与当前 VirtualBox/Ubuntu 支持状态。

## 0. 目标状态与阶段门

最终状态：

```text
微信 / 飞书 / CLI
        -> Ubuntu Hermes Gateway（唯一主实例）
             -> Ubuntu Mihomo
                  -> Mac 本地代理（首选）
                  -> Ubuntu 独立节点（代理备用）
             -> GPT-5.6 Sol（主模型）
             -> GPT-5.6 Luna（辅助任务）
             -> DeepSeek（模型备用）

阿里云 ECS
        -> 远程运维中转
        -> 每周脱敏核心数据备份
```

阶段门原则：

1. Ubuntu SSH 未稳定前，不安装 Hermes。
2. Hermes CLI 未完成一次正常对话前，不配置 Gateway。
3. Codex 两个端点未稳定可达前，不做设备授权。
4. CLI 模型调用未成功前，不切换微信/飞书。
5. 本地 Gateway 未验收前，不停用旧主节点。

## 1. Mac 与虚拟机基线

参考资源：

- Intel Mac，8GB RAM
- Ubuntu 24.04 LTS x86_64 Server
- VM 内存 5GB 左右，2 vCPU
- 动态磁盘 20GB 或更大
- Ubuntu 内 2GB Swap，`vm.swappiness=10`
- VirtualBox NAT，宿主回环端口转发到 Guest 22

必须先验证：

```bash
/Applications/VirtualBox.app/Contents/MacOS/VBoxManage --version
/Applications/VirtualBox.app/Contents/MacOS/VBoxManage \
  showvminfo <VM_NAME> --machinereadable | grep -E '^(VMState|memory|cpus|Forwarding)'
```

Ubuntu 内验证：

```bash
uname -m
free -h
swapon --show
df -h /
systemctl is-active ssh
```

## 2. Ubuntu 基础依赖

```bash
sudo apt update
sudo apt install -y \
  build-essential libatomic1 git curl ca-certificates \
  ripgrep rsync ffmpeg tmux unzip jq
```

`libatomic1` 对 Hermes 管理的 Node.js 很关键；若缺失，`node` 会报 `libatomic.so.1` 找不到。`build-essential` 用于编译 `node-pty` 等原生模块。

## 3. 安装 Hermes

使用普通运行用户执行官方 CLI 安装器，不要在 root Home 中建立生产实例：

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
source "$HOME/.bashrc"
command -v hermes
hermes --version
```

典型布局：

```text
~/.hermes/hermes-agent/       程序与 venv
~/.hermes/                    配置、记忆和运行数据
~/.local/bin/hermes           CLI 入口
```

基础检查：

```bash
hermes config check
hermes doctor
```

`doctor` 的大量并行 API 检查可能受中国大陆网络影响。若长期无输出，可中断后改用本指南的定向端点探测；不要把所有可选供应商失败都当成安装失败。

## 4. 浏览器与本地语音（可选）

浏览器：

```bash
cd "$HOME/.hermes/hermes-agent"
npx playwright install --with-deps chromium
timeout 90 npx playwright screenshot \
  --browser chromium https://example.com /tmp/hermes-browser-test.png
file /tmp/hermes-browser-test.png
```

在中国大陆下载 Playwright 很慢时，可以临时指定已验证的镜像；镜像 URL 与版本应在使用前重新校验，不能永久信任第三方分发。

本地 STT 可使用 `faster-whisper` 与本地模型。5GB VM 能加载 small/int8，但浏览器和 STT 不宜与多个长任务并发。

## 5. 先打通 Mac 代理复用

Mac 本地代理客户端应先满足：

1. 代理端口只监听 `127.0.0.1`。
2. Mac 自身可经该端口访问 OpenAI Auth 与 Codex。
3. 当前代理策略允许 ChatGPT/OpenAI 域名通过支持地区出口。

Mac 测试：

```bash
curl -4 -sS -o /dev/null \
  --proxy http://127.0.0.1:<MAC_PROXY_PORT> \
  --connect-timeout 15 --max-time 30 \
  -w 'HTTP=%{http_code} TLS=%{time_appconnect}s TOTAL=%{time_total}s\n' \
  https://chatgpt.com/backend-api/codex
```

安装 [`com.example.hermes-mac-proxy-tunnel.plist.example`](../configs/macos/com.example.hermes-mac-proxy-tunnel.plist.example) 前替换占位符并校验：

```bash
plutil -lint /tmp/com.example.hermes-mac-proxy-tunnel.plist
sudo install -o root -g wheel -m 644 \
  /tmp/com.example.hermes-mac-proxy-tunnel.plist \
  /Library/LaunchDaemons/com.example.hermes-mac-proxy-tunnel.plist
sudo launchctl bootstrap system \
  /Library/LaunchDaemons/com.example.hermes-mac-proxy-tunnel.plist
sudo launchctl kickstart -k system/com.example.hermes-mac-proxy-tunnel
```

Ubuntu 侧应出现回环监听：

```bash
ss -lnt | grep ':<GUEST_PROXY_PORT> '
```

## 6. 配置 Ubuntu Mihomo

Mihomo 只监听 Ubuntu 回环地址，配置分两层：

1. `MAC-SHADOWROCKET`：HTTP 代理指向 SSH remote-forward 端口。
2. `CODEX` fallback：Mac 代理优先；只有通过“两地区、每区至少两节点”真实端点验收后，才追加地区备用组。

合并 [`mihomo-codex-proxy.yaml.example`](../configs/ubuntu/mihomo-codex-proxy.yaml.example) 后：

```bash
mihomo -t -d "$HOME/.config/mihomo" \
  -f "$HOME/.config/mihomo/config.yaml"
sudo systemctl restart mihomo.service
systemctl is-active mihomo.service
```

生产配置修改必须使用“备份 → 离线校验 → 原子安装 → 重启 → 双端点测试 → 失败回滚”顺序。临时 probe 使用独立端口，结束后清理进程，避免端口占用导致假失败。当前两地区门槛尚未通过，所以地区备用组没有安装。

## 7. 验证 OpenAI/Codex 网络

通过 Ubuntu 生产 Mihomo：

```bash
curl -4 -sS -o /dev/null \
  --proxy http://127.0.0.1:<MIHOMO_PORT> \
  --connect-timeout 15 --max-time 30 \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  -w 'AUTH_HTTP=%{http_code} TOTAL=%{time_total}s\n' \
  https://auth.openai.com/api/accounts/deviceauth/usercode

curl -4 -sS -o /dev/null \
  --proxy http://127.0.0.1:<MIHOMO_PORT> \
  --connect-timeout 15 --max-time 30 \
  -w 'CODEX_HTTP=%{http_code} TOTAL=%{time_total}s\n' \
  https://chatgpt.com/backend-api/codex
```

基线预期：Device Auth POST 能拿到非 `000` HTTP 响应；未认证 Codex GET 常见 `403`。curl `28` 是超时，`35` 是 TLS 失败，均不算跑通。

## 8. ChatGPT Codex 设备授权

在 ChatGPT 安全设置中允许 Codex 设备代码授权，然后在 Ubuntu 执行：

```bash
HTTP_PROXY=http://127.0.0.1:<MIHOMO_PORT> \
HTTPS_PROXY=http://127.0.0.1:<MIHOMO_PORT> \
NO_PROXY=127.0.0.1,localhost \
hermes auth add openai-codex --label "ChatGPT subscription"

hermes auth list openai-codex
```

只记录 `logged in` 与凭据标签，不输出 `auth.json`。授权动作必须由用户在浏览器中完成。

## 9. 模型组合

目标组合：

- 主模型：`gpt-5.6-sol`，`openai-codex`
- 主推理：有效 `medium`
- 高频辅助：`gpt-5.6-luna`
- 模型备用：`deepseek-v4-flash-vision-exp`

使用 `hermes model` 选择主模型；使用 `hermes fallback add` 添加备用。辅助任务可参考 [`hermes-model-routing.yaml.example`](../configs/ubuntu/hermes-model-routing.yaml.example) 逐项用 `hermes config set` 写入。

每次修改前备份 `config.yaml`，完成后：

```bash
hermes config check
hermes config get model
hermes fallback list
```

## 10. Gateway 系统服务

```bash
sudo hermes gateway install \
  --system --run-as-user <VM_USER> --no-start-now
```

把 [`hermes-gateway-proxy.conf.example`](../configs/ubuntu/hermes-gateway-proxy.conf.example) 安装为 systemd drop-in，替换端口后：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-gateway.service
systemctl is-active hermes-gateway.service
systemctl is-enabled hermes-gateway.service
```

不要同时保留用户级和系统级两个 Gateway 服务。

## 11. 消息平台与最终验收

通过 `hermes gateway setup` 配置消息平台，凭据只进入受限本地文件。验收顺序：

1. CLI 固定文本真实调用成功。
2. Gateway 与 Mihomo 均 active/enabled。
3. 微信回复成功。
4. 飞书私聊回复成功。
5. 如启用群聊，单独验证 mention 与权限规则。
6. 检查无重复回复、无双主竞争。
7. 再停用旧主节点 Gateway。

## 12. 远程运维与备份

完成本地主节点后，再按以下文档配置：

- [`architecture.md`](architecture.md)：ECS 中转与单主原则
- [`operations.md`](operations.md)：健康检查与故障切换
- [`backup-and-restore.md`](backup-and-restore.md)：每周凭据排除备份
- [`security.md`](security.md)：最小权限和发布边界

至此才可宣布部署完成。
