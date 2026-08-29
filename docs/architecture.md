# Architecture

## 1. 三层职责

### macOS 宿主机

- 长期接通电源并关闭系统睡眠
- 运行 VirtualBox
- 通过 LaunchDaemon 自动启动 Ubuntu VM
- 主动连接 ECS，维持反向 SSH 隧道
- 运行用户选择的本地代理客户端
- 通过独立 LaunchDaemon 把本地代理端口反向转发到 Ubuntu 回环地址
- 不直接运行 Hermes 主服务

### Ubuntu 虚拟机

- 运行唯一的 Hermes Gateway
- 保存 Hermes 人格、记忆、Skills、会话与平台配置
- 运行 Playwright Chromium 和本地语音识别
- 通过 systemd 在开机后自动启动 Gateway
- 运行本地 Mihomo，仅把 OpenAI/Codex 域名送入受控故障组
- 首选复用 Mac Shadowrocket；独立地区备用组必须通过真实端点门槛后才能启用
- 每周主动向 ECS 发送核心数据备份

### 阿里云 ECS

- 提供 Workbench 登录入口
- 在专用 SSH 端口接收旧 Mac 的反向隧道
- 仅在回环地址监听映射到旧 Mac 的 SSH 端口
- 用专用强制命令账号接收备份
- 不运行生产 Hermes Gateway

## 2. 启动与恢复链路

```text
Mac 开机
  ├─ LaunchDaemon 启动反向 SSH 隧道
  └─ LaunchDaemon 启动 VirtualBox Ubuntu VM
         └─ Ubuntu systemd 启动 Hermes Gateway
                ├─ 微信适配器上线
                └─ 飞书 WebSocket 上线
```

网络中断时，SSH 的 KeepAlive 与 LaunchDaemon KeepAlive 负责重新建立隧道。VM 或 Ubuntu 服务异常时，分别由 VirtualBox/LaunchDaemon 与 systemd 恢复。

模型与代理链路独立于远程运维链路：

```text
Hermes Gateway
  -> Ubuntu Mihomo
       -> 首选：Ubuntu 回环代理端口
            -> SSH remote forward
                 -> Mac 本地代理端口
                      -> OpenAI/Codex
       -> 候选备用：两个通过验收的地区组（当前未上线）
```

Hermes 自身另有模型级故障链：Codex 主模型发生限流、5xx、认证或连接错误时，尝试 DeepSeek。代理故障组和模型故障链是两层不同机制，不能互相替代。

当前生产只验收了 Mac Shadowrocket 主路径。Ubuntu 地区备用组的实现存在，但最新受控探测未满足“两地区、每区至少两节点”的门槛，因此生产配置未启用该能力。

## 3. 远程维护路径

```text
管理员
  -> 阿里云 Workbench
  -> ECS 回环端口（反向隧道监听）
  -> macOS Remote Login
  -> macOS NAT 回环端口
  -> Ubuntu SSH
```

远程入口不应该把旧 Mac SSH 或 VM SSH 直接绑定到 ECS 公网地址。安全组只开放 ECS 的专用 SSH 入口，并优先限制来源地址。

## 4. 单主原则

微信和飞书适配器通常会维持长连接。若云端与本地 Gateway 同时启用，可能出现：

- 消息被两个进程竞争消费
- 重复回复或顺序错乱
- 会话与记忆写入分叉
- 平台登录状态互相挤占

因此任何故障切换都必须遵循：先停止当前主节点，再启动备用节点，最后做渠道验收。

## 5. 资源边界

5GB VM 内存适合个人低并发场景。浏览器、本地 STT 和 Agent 长任务可能同时达到较高峰值，建议：

- 保留 2GB Swap 作为突发保护，不把 Swap 当作常态内存
- 不并发运行多个重型浏览器任务
- STT 模型设置空闲卸载
- 定期检查 VM 磁盘、内存与 OOM 日志
