#!/usr/bin/env bash
# Slope Indicator Service 首次安装脚本
# 用法：bash deploy/install.sh
set -euo pipefail

PROJECT_DIR=/home/ops/slope_service
cd "$PROJECT_DIR"

# 1. venv
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 2. .env
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "[install] .env 已从 .env.example 拷贝，请手动编辑真实密码 / token"
fi

# 3. DB schema（幂等）
echo "[install] applying DB schema..."
PGPASSWORD="$(grep -E '^POSTGRES_PASSWORD=' .env | cut -d= -f2)" \
  psql -h 127.0.0.1 -U crawler -d predictlab -P pager=off -f db/schema.sql

# 4. systemd unit
sudo cp deploy/slope-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable slope-service.service

echo "[install] 完成。下一步："
echo "  1) 编辑 .env 里的 SLOPE_TOKEN"
echo "  2) sudo systemctl start slope-service"
echo "  3) curl http://127.0.0.1:8020/health"
