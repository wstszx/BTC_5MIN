#!/bin/bash
set -euo pipefail

# ============================================
# BTC 5min Bot - VPS 部署脚本 (Ubuntu 22.04)
# ============================================

BOT_USER=btcbot
BOT_DIR=/opt/btc-5min
CLASH_VERSION=v1.18.0  # 检查 https://github.com/MetaCubeX/mihomo/releases 获取最新

echo "=== 1. 系统更新 ==="
apt update && apt upgrade -y

echo "=== 2. 创建专用用户 ==="
id -u $BOT_USER &>/dev/null || useradd -m -s /bin/bash $BOT_USER

echo "=== 3. 安装依赖 ==="
apt install -y python3 python3-venv python3-pip git ufw curl unzip

echo "=== 4. 部署项目 ==="
mkdir -p $BOT_DIR
git clone https://github.com/your-org/btc-5min.git $BOT_DIR
cd $BOT_DIR

# Python 虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
# WebSocket over SOCKS5 支持
pip install PySocks websocket-client

echo "=== 5. 配置 .env.dashboard ==="
# 手动操作：从本地 scp 或手动编辑
# scp .env.dashboard root@vps:/opt/btc-5min/.env.dashboard
echo "--- 请手动将 .env.dashboard 复制到 $BOT_DIR/.env.dashboard ---"
echo "--- 并设置权限: chmod 600 $BOT_DIR/.env.dashboard ---"

echo "=== 6. 安装 Clash.Meta (mihomo) ==="
curl -L -o /tmp/clash-meta.zip \
  "https://github.com/MetaCubeX/mihomo/releases/download/$CLASH_VERSION/mihomo-linux-amd64-$CLASH_VERSION.gz"
gunzip -c /tmp/clash-meta.zip > /usr/local/bin/clash-meta
chmod +x /usr/local/bin/clash-meta

echo "=== 7. 配置 Clash ==="
# 手动操作: 将你的订阅链接填入 clash-config.yaml
echo "--- 编辑 $BOT_DIR/deploy/clash-config.yaml ---"
echo "--- 填入你的 V2Ray/Clash 订阅链接 ---"

echo "=== 8. 配置 systemd 服务 ==="
cp $BOT_DIR/deploy/clash.service /etc/systemd/system/
cp $BOT_DIR/deploy/btc-bot.service /etc/systemd/system/
systemctl daemon-reload

echo "=== 9. 防火墙 ==="
ufw allow ssh
ufw --force enable

echo "=== 10. 设置文件权限 ==="
chown -R $BOT_USER:$BOT_USER $BOT_DIR
chmod 600 $BOT_DIR/.env.dashboard 2>/dev/null || true

echo ""
echo "=== 部署完成 ==="
echo ""
echo "启动 Clash:   systemctl start clash"
echo "开机自启:     systemctl enable clash"
echo "检查状态:     systemctl status clash"
echo ""
echo "启动 Bot:     systemctl start btc-bot"
echo "开机自启:     systemctl enable btc-bot"
echo "查看日志:     journalctl -u btc-bot -f"
echo ""
echo "SSH 隧道访问 Dashboard:"
echo "  ssh -L 8787:127.0.0.1:8787 root@vps"
echo "  然后浏览器打开 http://127.0.0.1:8787"
