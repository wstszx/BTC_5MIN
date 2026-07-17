# BTC_5MIN VPS 部署文档

> 部署日期: 2026-07-17
> VPS: `<VPS_IP>` (RackNerd, Ubuntu 22.04)
> 代理出口: 加拿大 (OVH Quebec) IP 见 VPS 配置

---

## 1. 架构概览

```
┌─────────────────────────────────────────────┐
│  VPS <VPS_IP>                               │
│                                             │
│  ┌─────────────┐     ┌──────────────────┐   │
│  │  mihomo     │     │  btc-bot (python) │   │
│  │  clash.meta │     │  main.py          │   │
│  │  :7890      │◄────│  HTTP_PROXY       │   │
│  │  CA 节点    │     │  :8787 (dashboard)│   │
│  └──────┬──────┘     └──────────────────┘   │
│         │                                   │
│         ▼                                   │
│  Polymarket API (via 加拿大 IP)             │
└─────────────────────────────────────────────┘
```

| 组件 | 说明 |
|------|------|
| mihomo (clash.meta) | 代理客户端, 从订阅拉取节点, 选加拿大节点做出口 |
| btc-bot | 交易机器人, 通过 mihomo 代理访问 Polymarket |
| dashboard | 8787 端口, 仅监听 127.0.0.1 |
| update-sub.sh + cron | 每 12 小时自动更新订阅 |

---

## 2. 目录结构

| 路径 | 用途 |
|------|------|
| `/opt/btc-5min/` | 项目根目录 (git clone) |
| `/opt/btc-5min/.venv/` | Python 虚拟环境 |
| `/opt/btc-5min/.env.dashboard` | 运行配置 (含私钥, 权限 600) |
| `/opt/btc-5min/main.py` | 入口 |
| `/opt/btc-5min/deploy/mihomo-config.yaml` | mihomo 配置 |
| `/opt/btc-5min/deploy/update-sub.sh` | 订阅更新脚本 (cron 调用) |
| `/opt/btc-5min/deploy/update-sub.py` | 订阅更新 Python 逻辑 |
| `/opt/btc-5min/deploy/update-sub.log` | 更新日志 |
| `/usr/local/bin/mihomo` | mihomo 二进制 (compatible 版本) |
| `/etc/systemd/system/clash.service` | mihomo systemd 服务 |
| `/etc/systemd/system/btc-bot.service` | bot systemd 服务 |

---

## 3. systemd 服务

### 3.1 clash.service (mihomo 代理)

```ini
[Unit]
Description=Mihomo (Clash.Meta) Proxy
After=network.target

[Service]
Type=simple
User=btcbot
ExecStart=/usr/local/bin/mihomo -f /opt/btc-5min/deploy/mihomo-config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 3.2 btc-bot.service (交易机器人)

```ini
[Unit]
Description=BTC 5min Polymarket Trading Bot
After=network.target clash.service

[Service]
Type=simple
User=btcbot
WorkingDirectory=/opt/btc-5min
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/btc-5min/.venv/bin/python main.py
Restart=always
RestartSec=10
StartLimitBurst=5
NoNewPrivileges=true
ProtectSystem=full
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

---

## 4. 运行状态

| 项目 | 状态 |
|------|------|
| mihomo | active (running), 出口 IP 加拿大 (CA Quebec) |
| btc-bot | active (running), paper + live trading + dashboard |
| 纸面策略 | 5,6,8,9,12,13 |
| 实盘策略 | 7,11,10 |
| Dashboard | http://127.0.0.1:8787/ (仅本地) |
| 订阅自动更新 | cron 每 12 小时 (0 */12 * * *) |

---

## 5. 安全配置

| 项目 | 配置 |
|------|------|
| SSH | 仅密钥认证 (密码登录已禁) |
| root 密码 | 已改为强随机密码 |
| Dashboard 8787 | ufw deny, 仅 127.0.0.1 可访问 |
| .env.dashboard | 权限 600, 属主 btcbot |
| 防火墙 | ufw active, 仅放行 22/80/443 |
| 私钥 | 本地 `%USERPROFILE%\.ssh\vps_key` |

---

## 6. 日常运维命令

### 6.1 SSH 登录 + 访问 Dashboard

```powershell
# 本地 PowerShell
ssh -i "$env:USERPROFILE\.ssh\vps_key" -L 8787:127.0.0.1:8787 root@<VPS_IP>
```
然后浏览器打开 http://127.0.0.1:8787/

### 6.2 服务管理

```bash
# 状态
systemctl status clash
systemctl status btc-bot

# 重启
systemctl restart clash
systemctl restart btc-bot

# 日志
journalctl -u clash --no-pager -n 50
journalctl -u btc-bot --no-pager -n 50
journalctl -u btc-bot -f   # 实时跟踪
```

### 6.3 代理诊断

```bash
# 代理是否通
curl -x http://127.0.0.1:7890 -s -o /dev/null -w '%{http_code}' https://www.google.com

# 出口 IP
curl -x http://127.0.0.1:7890 -s https://ipinfo.io/json
```

### 6.4 更新代码

```bash
cd /opt/btc-5min
git pull origin main
pip install -r requirements.txt --quiet   # 如有新依赖
systemctl restart btc-bot
```

### 6.5 查看/修改配置

配置通过 Dashboard 修改后自动写回 `.env.dashboard`。也可直接编辑:

```bash
nano /opt/btc-5min/.env.dashboard
systemctl restart btc-bot
```

---

## 7. 更换订阅地址

如果需要更换代理订阅 URL:

```bash
ssh root@<VPS_IP>
nano /opt/btc-5min/deploy/update-sub.sh     # 改第 4 行 SUB_URL
nano /opt/btc-5min/deploy/update-sub.py     # 不用改, 它读 /tmp/clash_raw.yaml
bash /opt/btc-5min/deploy/update-sub.sh     # 立即生效
```

`update-sub.sh` 里的 `SUB_URL` 是唯一需要改的地方。改完执行该脚本就会拉新订阅 + 重建配置 + 重启 mihomo。cron 会按新 URL 自动跑。

---

## 8. 订阅自动更新机制

### 8.1 cron 任务

```cron
0 */12 * * * /opt/btc-5min/deploy/update-sub.sh >> /opt/btc-5min/deploy/update-sub.log 2>&1
```

每天 00:00 和 12:00 UTC 自动执行。

### 8.2 更新脚本逻辑

`update-sub.sh` 做的事:
1. 用 `curl -A 'clash.meta'` 拉取订阅 YAML
2. 调用 `update-sub.py` 解析:
   - 找 tag 含 "加拿" / "Canada" / "🇨🇦" 的节点
   - 过滤假节点 (server=127.0.0.1, info 类节点)
   - 构建 proxy-groups: CA-Auto (url-test) -> CA-Select -> PROXY
   - 所有流量走 PROXY (MATCH,PROXY)
3. `systemctl restart clash` 重启 mihomo
4. 测试代理是否通
5. 日志写到 `update-sub.log`

### 8.3 手动触发更新

```bash
bash /opt/btc-5min/deploy/update-sub.sh
```

### 8.4 查看更新日志

```bash
tail -50 /opt/btc-5min/deploy/update-sub.log
```

---

## 9. mihomo 配置说明

配置文件: `/opt/btc-5min/deploy/mihomo-config.yaml`

结构:
```yaml
mixed-port: 7890          # HTTP/SOCKS 混合代理端口
allow-lan: false           # 不允许局域网
bind-address: '127.0.0.1'  # 仅本机
mode: rule
log-level: info

proxies:                   # 从订阅拉取的所有真实节点
  - {name: '加拿大|AI推荐', type: anytls, ...}
  - ...

proxy-groups:
  - name: CA-Auto          # url-test, 自动选最快加拿大节点
    type: url-test
    proxies: ['加拿大 IPv6', '加拿大|AI推荐']
    url: 'https://www.gstatic.com/generate_204'
    interval: 300
    tolerance: 50
  - name: CA-Select       # 手动选加拿大节点
  - name: PROXY           # 总入口, 默认走 CA-Auto

rules:
  - MATCH,PROXY            # 所有流量走 PROXY
```

当前加拿大节点 (2 个):
- `加拿大 IPv6` (hysteria2) -> `<CA_IPV6>`
- `加拿大|AI推荐` (anytls) -> `<CA_IP>`:6001

---

## 10. 故障排查

### 10.1 btc-bot 启动失败: Address already in use

```bash
# 查端口占用
fuser 8787/tcp
# 杀掉占用进程
fuser -k 8787/tcp
# 重启
systemctl restart btc-bot
```

### 10.2 代理不通

```bash
# 检查 mihomo 状态
systemctl status clash
journalctl -u clash --no-pager -n 30

# 手动测试
curl -x http://127.0.0.1:7890 -v https://www.google.com 2>&1 | head -20

# 重启 mihomo
systemctl restart clash

# 手动更新订阅
bash /opt/btc-5min/deploy/update-sub.sh
```

### 10.3 mihomo 不支持某协议

如果订阅里有新协议 (如 anytls), 确保 mihomo 是 compatible 版本:
```bash
/usr/local/bin/mihomo -v
# 应显示 "with_gvisor" tag, 含 compatible
```

如果不支持, 重新下载:
```bash
LATEST=$(curl -s https://api.github.com/repos/MetaCubeX/mihomo/releases/latest | grep '"tag_name"' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
curl -L -o /tmp/mihomo.gz "https://github.com/MetaCubeX/mihomo/releases/download/${LATEST}/mihomo-linux-amd64-compatible-${LATEST}.gz"
gunzip -f /tmp/mihomo.gz
chmod +x /tmp/mihomo
mv /tmp/mihomo /usr/local/bin/mihomo
systemctl restart clash
```

### 10.4 彻底重装 mihomo 配置

```bash
# 1. 拉订阅
curl -s -A 'clash.meta' '<SUBSCRIPTION_URL>' -o /tmp/clash_raw.yaml

# 2. 跑更新脚本重建
bash /opt/btc-5min/deploy/update-sub.sh
```

---

## 11. 部署历史记录

本次部署过程中遇到并解决的问题:

| 问题 | 原因 | 解决 |
|------|------|------|
| subconverter 返回 0 字节 | 订阅 URL 中 `?token=` 被 subconverter 当参数吃掉 | 弃用 subconverter, 直接解析 Clash YAML |
| sing-box 不支持 anytls | sing-box 1.11.7 无 anytls 协议 | 换成 mihomo (clash.meta) compatible 版本 |
| sing-box "dependency[DIRECT] not found" | 清洗配置时误删 direct 出站 | 保留 direct/block (已在 update-sub.py 中处理) |
| btc-bot "Address already in use" | 旧的 /root/btc_5min 目录的进程占 8787 | 找到并删除旧的 btc_5min.service |
| mihomo "CA-Auto failed multiple times" | 加拿大 IPv6 节点不稳定 | 已配置 url-test 自动切换到可用的加拿大节点 |

---

## 12. 凭证与密钥位置

| 凭证 | 位置 | 备注 |
|------|------|------|
| SSH 私钥 | 本地 `%USERPROFILE%\.ssh\vps_key` | 唯一登录方式 |
| Polymarket 私钥 | VPS `/opt/btc-5min/.env.dashboard` (POLYMARKET_PRIVATE_KEY) | 权限 600 |
| 订阅 URL | VPS `/opt/btc-5min/deploy/update-sub.sh` 第 4 行 | 含 token |
| root 密码 | 强随机密码 (已改) | 推荐用 SSH key, 不用密码 |

---

## 13. 应急联系

如果 SSH 密钥丢失, 无法登录 VPS:
1. RackNerd 面板 -> VNC 控制台 (可直接进系统)
2. 面板 -> Reset Root Password
3. 用新密码通过 VNC 或 SSH 登录后重新配置密钥