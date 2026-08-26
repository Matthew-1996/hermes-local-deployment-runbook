# QA and Troubleshooting History

本页整理真实部署过程中遇到的症状、误判点和已验证恢复方法。示例均已脱敏；不要粘贴真实凭据、订阅或完整配置来复现。

## 1. 下载与安装

### Ubuntu ISO 下载速度变成 0

**判断：** 单次短 Range 测试速度正常，不代表 3GB 长下载稳定。CDN、跨境链路和连接复用会导致长传输停滞。

**处理：**

- 使用 `curl -C -` 断点续传。
- 设置合理的 `--speed-limit` 与 `--speed-time`，低速自动断开重试。
- 下载完成后用发行方 `SHA256SUMS` 校验；文件大小正确不能替代哈希。

### VirtualBox `unattended detect` 先报 `NS_ERROR_NOT_IMPLEMENTED`

若后续仍输出正确的 `OSTypeId`、版本和 `IsInstallSupported=on`，说明 ISO 识别信息已经得到。不要仅凭第一行错误删除 VM 或重下 ISO，应继续检查完整输出与 unattended 文件。

### VDI 长时间停在同一大小

动态 VDI 大小不等于安装进度。先检查 VM 状态、控制台截图和磁盘 I/O；若控制台已经出现 Ubuntu 登录提示与 cloud-init 完成，安装可能已结束，只是 SSH 尚未就绪。

## 2. Ubuntu SSH

### NAT 端口可连接，但 SSH `kex_exchange_identification` 被重置

如果宿主 `nc` 能连接 Guest 22 的 NAT 端口，而 SSH 握手被重置，常见原因是 Guest 内 `sshd` 尚未安装或未启动，不是 ECS 安全组。安全组不参与 `127.0.0.1` 的宿主到 Guest NAT 回环连接。

检查 unattended 配置是否包含 `openssh-server`。必要时通过 VM 控制台登录 Ubuntu 后安装并启用：

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
```

## 3. Hermes 安装依赖

### Node.js 报 `libatomic.so.1` 缺失

```bash
sudo apt install -y libatomic1
```

安装后重新运行 Hermes 安装器；不需要删除已经下载的 Node 目录。

### 安装器找不到 C++ 编译器

```bash
sudo apt install -y build-essential
```

不要把管理员密码写进脚本。确认当前 `whoami` 后，在 sudo 提示符手工输入对应账号密码。

### GitHub clone 长时间不动

先用 `curl` 定向测试 GitHub，而不是无限等待。可选恢复路径：

1. 修复代理/DNS 后重试 HTTPS clone。
2. 从可信机器打包同版本源码，通过 SSH 传输。
3. 两端 SHA-256 一致并验证 `tar -tzf` 后再解压。

源码归档不应包含 `.git`、venv、`node_modules`、`.env`。

### `hermes` 安装完成但新 Shell 找不到命令

```bash
source "$HOME/.bashrc"
command -v hermes
ls -l "$HOME/.local/bin/hermes"
```

如果当前提示符在 Mac 而 Hermes 安装在 Ubuntu，`hermes: command not found` 是环境层级错误，应先登录 Guest，不要在 Mac 重新安装一份生产 Hermes。

### 第一次模型调用看似卡在 boto3 lazy install

中国大陆访问 PyPI 不稳定时，Hermes 首次发现可选 provider 可能触发 lazy dependency。可以把依赖预装到 Hermes venv，而不是系统 Python：

```bash
"$HOME/.hermes/bin/uv" pip install \
  --python "$HOME/.hermes/hermes-agent/venv/bin/python" \
  "boto3==<PINNED_VERSION>"
```

版本应与当前 Hermes 日志请求一致。安装后用 venv Python import 验证。

## 4. 浏览器与 STT

### Playwright Chromium 长时间 0%

先对官方 CDN 和可接受的镜像做小 Range 测试。镜像明显更快时，可临时设置 `PLAYWRIGHT_DOWNLOAD_HOST` 后重新安装。安装完成必须用真实截图命令验证，看到缓存目录不等于浏览器可运行。

### faster-whisper 包存在但本地模型缺失

包和模型是两件事。若从另一台主机迁移模型：

1. 打包模型目录。
2. 两端校验 SHA-256。
3. 解压到配置中声明的绝对路径。
4. 用 `WhisperModel(..., device="cpu", compute_type="int8")` 做加载测试。
5. 记录峰值 RSS，避免与浏览器重任务并发。

## 5. Proxy、DNS 与 Codex

### Mac 开了“全局代理”，Ubuntu 出口仍是中国大陆

VirtualBox NAT 不会自动继承 macOS 系统代理或 TUN 策略。解决方法是显式代理：

- Mac 代理监听回环端口。
- Mac 通过 SSH `-R` 把该端口提供给 Ubuntu 回环地址。
- Ubuntu Mihomo 把 OpenAI/Codex 规则指向该代理。

### `HTTP=403` 到底是不是成功

- Codex 未认证 GET 返回 `403`：网络/TLS 已到达端点，属于成功的可达性探测。
- Device Auth POST 返回非 `000` 响应：端点可达，但仍需浏览器完成授权。
- `HTTP=000` + curl `28`：连接超时。
- `HTTP=000` + curl `35`：TLS 失败或上游连接被重置。

不要把 `401/403` 当作网络失败，也不要把它当作模型调用成功。

### 自建 Mihomo 配置中多数节点失败，但 Mac 客户端可用

常见原因：

- 自建配置丢失订阅的 DNS、策略组或传输字段。
- 节点服务器域名被污染或解析到不可达 IP。
- 只复制 `proxies`，没有保留订阅的完整规则链。
- Mac 客户端实际使用“自动选择/智能选择”，并非界面上以为的节点。

先用完整订阅配置建立基线，再逐步最小化。最终方案把 Mac 客户端作为首选代理，Ubuntu 只保留少量已验证备用，降低重复实现和漂移风险。

### 临时 Mihomo probe 报端口已占用

测试脚本异常退出后可能残留进程。先列出仅属于临时目录的 Mihomo PID 和监听端口，确认生产服务 PID 后再停止临时进程。不要使用模糊的 `pkill mihomo`，否则会误杀生产代理。

## 6. macOS LaunchDaemon

### `last exit code = 78: EX_CONFIG`

按顺序检查：

1. `plutil -lint` 是否通过。
2. plist 文件名和 Label 是否包含误粘贴的反斜杠。
3. `UserName`、`HOME`、私钥与日志目录所有者是否一致。
4. 程序绝对路径是否存在。
5. 目标普通用户能否在相同环境手工执行命令。

### Workbench 粘贴长 heredoc 后终端出现大量 `>` 和乱码

这是 Shell 仍在等待未闭合引号或 heredoc。按一次 `Ctrl+C`，确认提示符恢复，再用较短命令块。复杂 plist 可以先在本地生成并 `plutil` 校验，再安装；不要在损坏的续行提示符中继续输入 sudo 命令。

### sudo 一直提示密码错误

先执行：

```bash
whoami
id -Gn
```

Mac 的服务账号与管理员账号可能不同。普通服务账号没有 sudo 是正常安全边界；切换到管理员账号后再安装 LaunchDaemon。不要把 sudo 权限永久扩大给服务账号。

## 7. Codex 授权与模型

### 设备授权提示需要在 ChatGPT 安全设置启用

由用户在 ChatGPT 安全设置中允许 Codex 设备代码授权，再从 Ubuntu 执行 `hermes auth add openai-codex`。授权完成只用 `hermes auth list openai-codex` 检查状态，不读取 `auth.json`。

### Sol 已回复，但等待较长

先区分：

1. 代理冷连接时间。
2. Sol 主推理时间。
3. Luna 辅助任务是否串行执行。
4. 是否发生请求重试或 fallback。

本次基线中，Ubuntu 到隧道入口平均约 0.6ms，而完整 Codex 冷连接平均约 5.19s、范围约 1.86–8.51s。SSH 入口不是分钟级等待的主因。当前 Luna 辅助任务使用 `max`；若响应优先，应在用户确认后把高频任务降为 `medium`。

## 8. Gateway 与消息渠道

### CLI 成功，微信/飞书却没有回复

CLI 不经过生产消息适配器。检查：

```bash
systemctl is-active hermes-gateway.service
systemctl show hermes-gateway.service -p Environment
journalctl -u hermes-gateway.service --since '10 minutes ago' --no-pager
```

确认系统服务包含代理环境、平台凭据可读、WebSocket 已连接。微信和飞书必须分别验收。

### 飞书一直显示“正在输入”

先确认模型调用是否仍在进行，再检查发送失败和格式 fallback。可以关闭工具进度、流式输出和中间旁白，但不能因此掩盖真正的模型或发送超时。

## 9. 备份与恢复

### 为什么云端备份不再包含 `.env` 和 `auth.json`

它们包含模型、平台与 OAuth 凭据。即使 SSH 传输加密，ECS 上的归档仍是静态敏感副本。当前策略选择“核心数据可恢复 + 凭据重新授权”，降低云端泄漏面。

恢复后应重新运行 provider 与消息平台授权，再进行模型和渠道验收。若未来必须备份凭据，先做客户端加密，不能仅依赖归档 `0600`。

## 10. 排障记录模板

每次新增案例记录以下字段，方便后续优化：

```text
时间与版本：
影响层级：Mac / VM / Ubuntu / Mihomo / Hermes / Channel / ECS
用户症状：
首个可靠证据：
被排除的误判：
根因：
可逆修复：
验证命令与结果：
是否需要更新模板或自动化：
是否涉及凭据（只写是/否，不写值）：
```
