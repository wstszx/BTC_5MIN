# VPS 部署指南

将 BTC_5MIN 交易机器人部署到美国 VPS，通过加拿大代理节点访问 Polymarket。

---

## 1. 架构概览

```
┌─────────────────────────────────────────┐
│           美国 VPS (Linux)               │
│                                          │
│  ┌──────────┐     ┌──────────────────┐  │
│  │ Clash /   │────▶│ 加拿大代理出口    │  │
│  │ V2Ray     │     │ (你的订阅节点)    │  │
│  └────┬─────┘     └──────────────────┘  │
│       │                                  │
│       ▼                                  │
│  ┌──────────────────────────────────┐   │
│  │  BTC_5MIN 交易机器人              │   │
│  │  - HTTP API → Polymarket (通过代理)│   │
│  │  - WebSocket → Polymarket (通过代理)│   │
│  │  - WebSocket → Binance (通过代理)   │   │
│  └──────────────────────────────────┘   │
│                                          │
│  ┌────────────┐                          │
│  │ Dashboard  │ ◀── SSH 隧道 (localhost) │
│  │ :8787      │     你本机浏览器访问     │
│  └────────────┘                          │
└─────────────────────────────────────────┘
```

### 关键通信路径

| 目标 | 协议 | 代理方式 | 说明 |
|------|------|---------|------|
| Polymarket REST API | HTTPS | mihomo TUN 透明代理 | 全流量自动走代理 |
| Polymarket WebSocket | WSS | mihomo TUN 透明代理 | 全流量自动走代理 |
| Binance WebSocket | WSS | mihomo TUN 透明代理 | 全流量自动走代理 |
| 仪表盘 | HTTP | 本地绑定，SSH 隧道 | 绑定 `127.0.0.1:8787` |

---

## 2. 代理方式：mihomo TUN 模式

在 VPS 上安装 mihomo，开启 **TUN 模式**（透明代理）。所有流量自动经过加拿大节点，**无需修改项目代码**。

**优点：**
- 零代码修改
- HTTP、WebSocket、DNS 全部走代理
- 系统级，不依赖应用配置

**缺点：**
- 代理失效时所有网络不可用（mihomo 可配置 fallback 策略）

---

## 3. VPS 基础环境搭建

### 3.1 系统要求

- **OS**: Ubuntu 22.04 LTS 或 Debian 12
- **Python**: 3.10+（项目要求）
- **内存**: ≥ 2GB（建议 4GB）
- **磁盘**: ≥ 20GB

### 3.2 初始设置

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装 Python 和工具
sudo apt install -y python3 python3-pip python3-venv git curl wget unzip

# 创建专用用户（禁止登录 shell）
sudo useradd -r -s /usr/sbin/nologin -m -d /opt/btc_bot btcbot
```

### 3.3 部署项目文件

```bash
# 方式一：从 Git 克隆（推荐）
sudo mkdir -p /opt/btc_bot
sudo git clone <你的仓库地址> /opt/btc_bot/app
sudo chown -R btcbot:btcbot /opt/btc_bot

# 方式二：从本机 scp
# scp -r /path/to/BTC_5MIN user@vps:/opt/btc_bot/app

# 创建 Python 虚拟环境
cd /opt/btc_bot/app
sudo -u btcbot python3 -m venv .venv
sudo -u btcbot .venv/bin/pip install --upgrade pip
sudo -u btcbot .venv/bin/pip install -r requirements.txt
```

---

## 4. 安装 mihomo (Clash Meta) TUN 模式

### 4.1 安装 Clash Meta (mihomo)

```bash
# 下载最新 mihomo (Clash Meta)
# 从 https://github.com/MetaCubeX/mihomo/releases 获取最新版本
wget https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-v1.18.10.gz
gunzip mihomo-linux-amd64-v1.18.10.gz
chmod +x mihomo-linux-amd64-v1.18.10
sudo mv mihomo-linux-amd64-v1.18.10 /usr/local/bin/mihomo
```

### 4.2 配置 mihomo（开启 TUN 模式）

创建 `/etc/clash/config.yaml`：

```yaml
# 混合端口 (HTTP + SOCKS5)
mixed-port: 7890

allow-lan: false
bind-address: "127.0.0.1"
log-level: info

# TUN 模式 — 透明代理，系统所有流量自动走代理
tun:
  enable: true
  stack: system        # Linux 推荐 system，也可用 gvisor
  dns-hijack:
    - any:53
  auto-route: true     # 自动添加路由，避免 TUN 影响本机回环地址

# DNS 配置（避免 DNS 污染）
dns:
  enable: true
  listen: 0.0.0.0:53
  default-nameserver:
    - 8.8.8.8
    - 1.1.1.1

# 模式: Rule
mode: Rule

# 你的订阅节点
proxies:
  - name: "Canada-1"
    type: ss
    server: your-server.com
    port: 443
    cipher: chacha20-ietf-poly1305
    password: "your-password"

# 策略组
proxy-groups:
  - name: "Proxy"
    type: select
    proxies:
      - "Canada-1"

# 规则：全部走代理
rules:
  - MATCH,Proxy
```

> 💡 有订阅链接的话：`curl -o /etc/clash/config.yaml "订阅链接"`，然后手动补上 `tun:` 和 `dns:` 配置段。

### 4.3 创建 Clash systemd 服务

`/etc/systemd/system/clash.service`：

```ini
[Unit]
Description=Clash Proxy
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/mihomo -d /etc/clash
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable clash
sudo systemctl start clash
sudo systemctl status clash
```

### 4.4 验证代理可用

```bash
# 测试代理
curl -x http://127.0.0.1:7890 -s https://ipinfo.io/json | grep -E '"ip"|"country"'
# country 应为 "CA"
```

---

## 5. TUN 模式下不需要设置代理环境变量

mihomo TUN 模式在系统层面透明代理所有流量，应用无需感知代理存在。**不需要设置 `HTTP_PROXY` 环境变量**，也不需要对项目做任何代码修改。

验证 TUN 模式生效：

```bash
# 在 VPS 上直接请求（不指定代理），应返回加拿大 IP
curl -s https://ipinfo.io/json | grep -E '"ip"|"country"'
# country 应为 "CA"
```

---

## 6. 环境变量与密钥配置

### 6.1 创建 `.env.dashboard`

```bash
sudo -u btcbot cp /opt/btc_bot/app/.env.dashboard.example /opt/btc_bot/app/.env.dashboard
sudo -u btcbot chmod 600 /opt/btc_bot/app/.env.dashboard
```

编辑 `.env.dashboard`，填入真实值：

```ini
# 实盘开关
LIVE_TRADING_ENABLED=true
TRADE_MODE=both

# --- 凭证（填写真实值）---
POLYMARKET_PRIVATE_KEY=0x你的私钥
POLYMARKET_FUNDER=0x你的钱包地址
POLYMARKET_CHAIN_ID=137
POLYMARKET_SIGNATURE_TYPE=0
POLYMARKET_ORDER_TYPE=FOK

# 策略配置（按需修改）
STRATEGY_7_BASE_ORDER_COST=1.2
# ... 其他参数
```

---

## 7. 安全加固

### 7.1 文件权限

```bash
# 私钥文件：仅 owner 可读写
sudo chmod 600 /opt/btc_bot/app/.env.dashboard
sudo chmod 600 /opt/btc_bot/app/.env.dashboard.lock  # 如果存在

# 日志目录：包含订单信息，不应公开
sudo chmod 750 /opt/btc_bot/app/logs

# 整个项目目录：btcbot 用户专有
sudo chown -R btcbot:btcbot /opt/btc_bot
```

### 7.2 防火墙

```bash
# 只开放必要端口
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow ssh  # SSH 端口
sudo ufw enable

# 确认仪表盘绑定 localhost 仅本机可访问
# 检查 main.py 中仪表盘监听地址是否为 127.0.0.1
```

### 7.3 SSH 隧道访问仪表盘（推荐）

不要将仪表盘端口暴露到公网。通过 SSH 隧道访问：

```bash
# 在本机执行（不要写密码到命令行）
ssh -L 8787:127.0.0.1:8787 user@your-vps-ip

# 然后浏览器访问 http://127.0.0.1:8787
# 流量通过 SSH 加密隧道
```

如果需要长期隧道，使用 `autossh`：

```bash
# 安装 autossh（macOS）
brew install autossh
# 或 Linux
sudo apt install autossh

# 自动重连隧道
autossh -M 0 -N -L 8787:127.0.0.1:8787 user@your-vps-ip -o "ServerAliveInterval=60" -o "ServerAliveCountMax=3"
```

### 7.4 systemd 安全加固

在 `btcbot.service` 中添加安全配置：

```ini
[Service]
# ... 基础配置 ...

# 安全加固
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
ReadWritePaths=/opt/btc_bot/app/logs /opt/btc_bot/app/.env.dashboard
ReadOnlyPaths=/opt/btc_bot/app
```

---

## 8. systemd 服务配置

`/etc/systemd/system/btcbot.service`：

```ini
[Unit]
Description=BTC 5MIN Trading Bot
After=network.target clash.service
Wants=clash.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=btcbot
Group=btcbot
WorkingDirectory=/opt/btc_bot/app
ExecStart=/opt/btc_bot/app/.venv/bin/python main.py
Restart=on-failure
RestartSec=30

# 日志
StandardOutput=append:/opt/btc_bot/app/logs/service_stdout.log
StandardError=append:/opt/btc_bot/app/logs/service_stderr.log

# 安全
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true
ReadWritePaths=/opt/btc_bot/app/logs /opt/btc_bot/app/.env.dashboard

[Install]
WantedBy=multi-user.target
```

> `Environment=` 行仅在 **bot 进程** 中设置代理环境变量，不影响 VPS 上的其他 systemd 服务。

### 启用服务

```bash
sudo systemctl daemon-reload
sudo systemctl enable btcbot
sudo systemctl start btcbot

# 查看状态
sudo systemctl status btcbot

# 查看日志
sudo journalctl -u btcbot -f
```

---

## 9. 验证部署

### 9.1 检查代理连通性

```bash
# 确认所有外部请求都走加拿大 IP
curl -s https://ipinfo.io/json | grep -E '"ip"|"country"'
# country 应为 "CA"
```

### 9.2 检查机器人日志

```bash
# 查看运行日志
sudo journalctl -u btcbot -n 100 --no-pager

# 或直接查看日志文件
tail -f /opt/btc_bot/app/logs/service_stdout.log
```

### 9.3 访问仪表盘

```bash
# 在本机建立 SSH 隧道
ssh -L 8787:127.0.0.1:8787 user@vps-ip

# 浏览器打开
open http://127.0.0.1:8787
```

### 9.4 检查成交记录

```bash
# 确认有新的成交数据
tail -f /opt/btc_bot/app/logs/live_orders.csv
```

---

## 10. 故障排除

### 10.1 TUN 模式不工作

```bash
# 检查 mihomo 是否运行
sudo systemctl status clash

# 检查 mihomo 日志
sudo journalctl -u clash -n 50 --no-pager

# 测试网络是否走代理（不指定代理，看 IP 是否加拿大）
curl -s https://ipinfo.io/json | grep -E '"ip"|"country"'
```

### 10.2 TUN 模式与其他服务冲突

如果 VPS 上其他服务因 TUN 模式出现问题（例如某些服务需要直连），可以在 mihomo 配置中添加规则，让特定流量绕过代理：

```yaml
rules:
  # 特定 IP/域名走直连
  - DOMAIN-SUFFIX,internal-service.com,DIRECT
  - IP-CIDR,10.0.0.0/8,DIRECT
  - IP-CIDR,172.16.0.0/12,DIRECT
  - IP-CIDR,192.168.0.0/16,DIRECT
  # 其余走代理
  - MATCH,Proxy
```

### 10.3 进程意外退出

```bash
# 查看最后 100 条 journal
sudo journalctl -u btcbot -n 100 --no-pager

# 检查系统资源
htop
free -m
df -h
```

---

## 11. 日常维护

### 11.1 更新代码

```bash
cd /opt/btc_bot/app
sudo -u btcbot git pull
sudo -u btcbot .venv/bin/pip install -r requirements.txt --upgrade
sudo systemctl restart btcbot
```

### 11.2 更新代理订阅

```bash
# 如果使用订阅链接
curl -o /etc/clash/config.yaml "你的订阅链接"
sudo systemctl restart clash
```

### 11.3 查看关键指标

```bash
# 最近成交
tail -20 /opt/btc_bot/app/logs/live_orders.csv

# Session 状态
cat /opt/btc_bot/app/logs/live_session_state.json
```

---

## 12. 注意事项

1. **API 请求频率**：Polymarket 对单 IP 有频率限制，代理共享 IP 可能触发限频
2. **WebSocket 保活**：如果使用方案 B/C，WebSocket 可能因代理超时断开，`websocket-client` 默认有重连机制
3. **时区**：VPS 默认 UTC 时区，日志时间戳为 UTC，与本机 +8 时区差 8 小时
4. **磁盘空间**：日志文件会持续增长，建议设置 logrotate
5. **私钥安全**：`.env.dashboard` 权限务必设置为 `600`，不要复制到共享环境
