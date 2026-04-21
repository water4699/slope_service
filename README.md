# Slope Indicator Service

Polymarket 策略 PnL 斜率熔断闸门服务。独立于交易策略的微服务 —— 任何策略都可通过 HTTP
API 查询"最近 N 笔已结算信号的累计 PnL 斜率"，斜率转负时拒绝下单。

## 设计目标

| 目标 | 实现 |
|---|---|
| **与策略解耦** | 独立进程、独立 repo，PredictLab 通过 HTTP 调用 |
| **复用 PG 避免双写** | 直接读 `median_trend_signals` + `median_trend_orders`，不维护自己的事实表 |
| **归一化仓位** | 全部按 `@100U` 计算 PnL（`100/lim - 100` 或 `-100`） |
| **前端可改 N** | N 值写在 `median_trend_risk_config.slope_n` 列，配合 PredictLab 的 RiskConfigCard 热更新 |
| **Fail-close** | 服务宕机 / 数据不足 → 调用方按"拒绝下单"兜底 |

## 架构

```
PredictLab (median_trend_engine)
        │
        │ GET /allow/btc_5m
        ▼
┌─────────────────────────────┐
│  Slope Service (FastAPI)    │
│   :8020                     │
│                             │
│  - GET  /allow/{variant}    │
│  - GET  /status             │
│  - POST /recompute          │
│  - GET  /health             │
└────────────┬────────────────┘
             │
             ▼
┌─────────────────────────────┐
│  Postgres (predictlab DB)   │
│                             │
│  读：median_trend_signals    │
│      median_trend_orders    │
│      median_trend_risk_config.slope_n │
│  写：slope_cache             │
└─────────────────────────────┘
```

## 快速上手

```bash
# 1. 克隆 + venv
git clone <repo> /home/ops/slope_service
cd /home/ops/slope_service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 配置 .env
cp .env.example .env
vim .env    # 填 POSTGRES_PASSWORD 和 SLOPE_TOKEN

# 3. DB schema
PGPASSWORD=xxx psql -h 127.0.0.1 -U crawler -d predictlab -f db/schema.sql

# 4. 启动（开发模式）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8020

# 5. 冒烟
curl http://127.0.0.1:8020/health
curl http://127.0.0.1:8020/allow/btc_5m
curl "http://127.0.0.1:8020/status?variant=btc_5m&n=50"
```

或直接跑一条：
```bash
bash deploy/install.sh
sudo systemctl start slope-service
```

## CLI

```bash
# 查所有已启用 variant 的状态
python -m app.cli status

# 强制重算所有
python -m app.cli recompute

# 30s 轮询监控
python -m app.cli watch
```

## HTTP API

### `GET /allow/{variant}?n=<N>`

下单决策闸门。`n` 留空则读 `median_trend_risk_config.slope_n`。

```json
{
  "variant": "btc_5m",
  "allow_trade": false,
  "n_window": 50,
  "n_in_window": 50,
  "slope_value": -12.34,
  "warmup": false,
  "enabled": true,
  "reason": "slope<0(-12.34)"
}
```

`enabled=false` 表示 `slope_n IS NULL`（前端未配置 N），永远 `allow_trade=true`。

### `GET /status?variant=<V>&n=<N>`

完整状态：N、slope、窗口内笔数、最近信号时间、计算耗时。

### `POST /recompute`

带 `X-Slope-Token` header，强制重算所有 slope_n 非 NULL 的 variant。

### `GET /health`

含 DB 连通性。

## 关键约束（方案 A）

只有 **同时满足** 下列条件的信号参与 slope 计算：

- `median_trend_signals.allowed = TRUE`
- `median_trend_signals.dry_run = FALSE`
- `median_trend_signals.limit_price IS NOT NULL`
- 对应的 `median_trend_orders.settlement_outcome IN ('UP', 'DOWN')`（已结算且非取消）
- `median_trend_orders.dry_run = FALSE`

被 slope gate 拦下来的信号（引擎里降级为 SKIP）**不推进 slope**，但别的 allowed=TRUE
信号会继续推进，因此"slope 永远负" 的陷阱不成立。

## Fail-close 约定

- `default_warmup_allow=False`：warmup 期 / n_in_window < N 时返回 `allow_trade=false`
- PredictLab 侧调用 timeout 超时时也应按 `allow_trade=false` 兜底
- 前端显式设置 `slope_n=NULL` 才完全禁用 gate
