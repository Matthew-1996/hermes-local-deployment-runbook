# Models and Proxy Routing

本文记录已经实际跑通的模型组合与 Mac 代理复用方案。所有配置均为脱敏模板，不应从真实机器直接复制凭据或订阅内容到仓库。

## 1. 模型职责

### 主模型

- Provider：`openai-codex`
- Model：`gpt-5.6-sol`
- Base URL：`https://chatgpt.com/backend-api/codex`
- Reasoning：有效默认值为 `medium`；建议在配置中显式固定，便于审计
- Auth：ChatGPT 设备代码授权，凭据保存在本机 `auth.json`，不得提交

已完成一次真实 CLI 调用和生产消息渠道验收。仅访问 Codex URL 得到 `403` 只能证明网络可达，不能证明模型授权或推理成功。

### 辅助模型

以下高频辅助任务固定到 `gpt-5.6-luna`：

```text
web_extract, compression, skills_hub, approval, mcp,
title_generation, triage_specifier, profile_describer,
goal_judge, monitor
```

当前配置使用 `reasoning_effort: max`。这保留了更深的辅助推理，但会削弱 Luna 的低延迟优势；若生产响应时间优先，建议把高频任务降为 `medium`，把 `max` 只留给确实需要深判断的任务。修改必须经过用户确认。

### 跨提供商备用

Hermes 顶层 `fallback_providers` 使用 DeepSeek V4 Flash Vision Exp。主模型发生限流、5xx、认证失败、网络中断或无效响应时，Hermes 按链路尝试备用；后续新回合仍优先恢复主模型。

备用配置存在不等于故障切换已实际触发。生产验收应分别记录“配置校验”“备用端点可达”和“受控故障切换”三个证据等级。

## 2. 脱敏模型配置

参考 [`configs/ubuntu/hermes-model-routing.yaml.example`](../configs/ubuntu/hermes-model-routing.yaml.example)。不要覆盖完整 `config.yaml`；应把示例字段合并到现有配置，并在修改前备份。

推荐流程：

```bash
cp -p "$HOME/.hermes/config.yaml" \
  "$HOME/.hermes/config.yaml.before-model-routing-$(date +%Y%m%d-%H%M%S)"

hermes config check
sudo systemctl restart hermes-gateway.service
systemctl is-active hermes-gateway.service
```

## 3. 代理拓扑

```text
Hermes/OpenAI client
  -> Ubuntu Mihomo loopback port
       -> CODEX fallback group
            1. MAC-LOCAL-PROXY
               -> Ubuntu loopback remote-forward port
               -> SSH connection to Mac host
               -> Mac local proxy loopback port
            2. <FALLBACK_NODE_1>
            3. <FALLBACK_NODE_2>
```

关键边界：

- Mac 客户端可以保持规则或智能选择模式；Ubuntu 继承它对 OpenAI/Codex 域名的实际选择。
- Ubuntu 只能观察出口国家、HTTP 状态和时延，不能可靠获知 Mac 客户端内部节点名称。
- SSH 隧道只绑定两端回环地址，不向局域网或公网开放代理。
- Ubuntu 独立备用节点只写在本机真实配置中，仓库仅使用占位符。

## 4. Mac 到 Ubuntu 的代理隧道

参考 [`configs/macos/com.example.hermes-mac-proxy-tunnel.plist.example`](../configs/macos/com.example.hermes-mac-proxy-tunnel.plist.example)。LaunchDaemon 以普通 Mac 服务用户运行：

```text
-R 127.0.0.1:<GUEST_PROXY_PORT>:127.0.0.1:<MAC_PROXY_PORT>
```

这会在 Ubuntu 创建仅回环监听的代理入口，并把新连接送到 Mac 本地代理。安装前应：

1. 替换所有占位符。
2. 确认专用 SSH 私钥为 `0600`，目标主机指纹已写入 `known_hosts`。
3. 使用 `plutil -lint` 校验 plist。
4. 确认 LaunchDaemon 的 `UserName` 是 VM/密钥所有者对应的普通 Mac 用户。
5. 使用 `launchctl print` 和 Ubuntu `ss -lnt` 双向验收。

## 5. Ubuntu Mihomo 故障组

参考 [`configs/ubuntu/mihomo-codex-proxy.yaml.example`](../configs/ubuntu/mihomo-codex-proxy.yaml.example)。示例是合并片段，不是完整订阅：

- `MAC-LOCAL-PROXY` 为首选。
- 两个占位备用节点必须已存在于本机 `proxies` 或 provider 中。
- 使用真实 Codex HTTPS URL 做健康检查。
- `expected-status: 200-499` 接受未认证探测的 `403`。
- `lazy: true` 避免无 Codex 流量时持续测速。

修改生产配置时必须先备份、离线校验、重启服务、执行 Auth/Codex 双端点测试；任何阶段失败都回滚。

## 6. 延迟基线

一次 5 次冷连接测试的观测值：

| 指标 | 结果 |
|---|---:|
| Ubuntu 连接回环隧道入口平均值 | 约 0.6ms |
| TLS ready 平均值 | 约 4.36s |
| First byte 平均值 | 约 4.84s |
| Total 平均值 | 约 5.19s |
| Total 中位数 | 约 4.11s |
| Total 范围 | 约 1.86–8.51s |

结论：Ubuntu 回环入口和 SSH 隧道建立不是主要瓶颈，波动主要发生在 Mac 代理出口到 Codex 的海外链路。该测试每次创建新连接，长驻 Gateway 的连接复用可能更快。分钟级回复等待还应检查 Sol 推理时间、Luna 辅助任务强度和是否发生重试。

## 7. 验收与排障

按以下顺序定位：

1. `ss -lnt`：Ubuntu 的 Guest 代理回环端口是否监听。
2. `launchctl print`：Mac 代理隧道 LaunchDaemon 是否 running。
3. `curl --proxy http://127.0.0.1:<GUEST_PROXY_PORT>`：Codex 是否返回预期 HTTP 状态。
4. `systemctl is-active mihomo.service`：生产代理是否 active。
5. `hermes auth list openai-codex`：只确认登录状态，不输出凭据。
6. `hermes -z`：执行一次真实主模型调用。
7. 微信或飞书：执行生产渠道最终回复测试。

不要通过粘贴 `.env`、`auth.json`、订阅 YAML 或完整 Mihomo 日志来排障。
