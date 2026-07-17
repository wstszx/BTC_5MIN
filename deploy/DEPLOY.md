# BTC 5min Bot VPS 部署手册

## 前提

- 一台 Ubuntu 22.04 VPS
- 一个 V2Ray/Clash 订阅链接（含目标国家节点）
- 项目代码已传到你的 Git 仓库

---

## 第一步：VPS 初始化

```bash
# SSH 登录 VPS
ssh root@<你的VPS_IP>

# 系统更新
apt update && apt upgrade -y

# 安装基础依赖
apt install -y python3 python3-venv python3-pip git ufw curl unzip

# 创建专用用户
useradd -m -s /bin/bash btcbot
```

---

## 第二步：部署项目代码

```bash
# 克隆项目
mkdir -p /opt/btc-5min
git clone <你的仓库地址> /opt/btc-5min

# Python 虚拟环境
python3 -m venv /opt/btc-5min/.venv
source /opt/btc-5min/.venv/bin/activate
pip install --upgrade pip
pip install -r /opt/btc-5min/requirements.txt

# WebSocket 走 SOCKS5 代理需要
pip install PySocks websocket-client
```

---

## 第三步：传入凭证文件

**在你的本地电脑（Windows）上执行：**

```powershell
# 用 scp 把 .env.dashboard 传到 VPS
scp D:\Projects\python\BTC_5MIN\.env.dashboard root@<VPS_IP>:/opt/btc-5min/.env.dashboard
```

**回到 VPS：**

```bash
# 锁定权限，只有 btcbot 能读
chmod 600 /opt/btc-5min/.env.dashboard
chown btcbot:btcbot /opt/btc-5min/.env.dashboard
```

---

## 第四步：安装 Clash.Meta（代理客户端）

```bash
# 下载最新版 mihomo
curl -L -o /tmp/clash-meta.gz \
  "https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-v1.18.10.gz"

# 解压到 /usr/local/bin
gunzip -c /tmp/clash-meta.gz > /usr/local/bin/clash-meta
chmod +x /usr/local/bin/clash-meta

# 验证安装
clash-meta -v
```

如果上面的版本号失效，去 https://github.com/MetaCubeX/mihomo/releases 查最新版。

---

## 第五步：配置代理

```bash
# 复制配置模板
cp /opt/btc-5min/deploy/clash-config.yaml.example /opt/btc-5min/deploy/clash-config.yaml

# 编辑配置，填入订阅链接
nano /opt/btc-5min/deploy/clash-config.yaml
```

把 `<你的订阅链接>` 替换成你的 V2Ray/Clash 订阅地址。

**测试代理：**

```bash
# 用 btcbot 用户测试
sudo -u btcbot clash-meta -d /opt/btc-5min/deploy -f /opt/btc-5min/deploy/clash-config.yaml

# 新开一个终端窗口，测试代理是否正常工作
curl -x http://127.0.0.1:7890 https://www.google.com
```

测试成功后按 Ctrl+C 停掉 Clash。

---

## 第六步：配置 systemd 守护进程

```bash
# 复制服务文件
cp /opt/btc-5min/deploy/clash.service /etc/systemd/system/
cp /opt/btc-5min/deploy/btc-bot.service /etc/systemd/system/

# 设置文件所有权
chown -R btcbot:btcbot /opt/btc-5min

# 重载 systemd
systemctl daemon-reload
```

---

## 第七步：防火墙

```bash
# 只开 SSH，不对外暴露任何端口
ufw allow ssh
ufw enable
ufw status
# 输出应该只显示 OpenSSH
```

---

## 第八步：启动

```bash
# 先启动代理（确保网络正常再启动 bot）
systemctl enable --now clash
systemctl status clash   # 确认 active (running)

# 检查 Clash 日志
journalctl -u clash -f --no-tail
# 看到类似于 "inbound=tcp in port=7890" 就说明正常运行
# 按 Ctrl+C 退出日志查看

# 再启动 bot
systemctl enable --now btc-bot
systemctl status btc-bot  # 确认 active (running)

# 查看 bot 日志
journalctl -u btc-bot -f --no-tail
```

---

## 第九步：查看 Dashboard

```bash
# SSH 隧道转发（在本地电脑上执行，不是 VPS）
ssh -L 8787:127.0.0.1:8787 root@<VPS_IP> -N
```

保持该终端开着。浏览器打开 `http://127.0.0.1:8787`。

---

## 常用运维命令

```bash
# 查看 bot 实时日志
journalctl -u btc-bot -f

# 查看最近 100 行日志
journalctl -u btc-bot --no-tail -n 100

# 重启 bot（修改配置后）
systemctl restart btc-bot

# 查看 Clash 日志
journalctl -u clash -f

# 更新代码
cd /opt/btc-5min && git pull
source .venv/bin/activate && pip install -r requirements.txt
systemctl restart btc-bot

# 停止 bot
systemctl stop btc-bot
```

---

## 安全注意事项

- `.env.dashboard` 包含私钥，已在 VPS 上设 `chmod 600`
- Dashboard 只绑 `127.0.0.1`，不对外暴露
- 防火墙只开放了 SSH 端口（22）
- VPS 上的代理和 bot 都使用非 root 用户（btcbot）运行
- 如果不再需要 VPS 访问，可以关掉 SSH 密码登录，改用 SSH key 认证
