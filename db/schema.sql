-- ============================================================================
-- Slope Indicator Service - DB schema
--
-- 本服务复用 PredictLab 的 Postgres 数据库（database=predictlab），但建自己的
-- 专属事实表 slope_signal_outcomes，结构对齐文档（文档原版是 SQLite）。
--
-- 执行：
--   psql -h 127.0.0.1 -U crawler -d predictlab -f db/schema.sql
-- ============================================================================


-- ── slope_signal_outcomes ──────────────────────────────────────────────────
-- 文档事实表（原版 SQLite signal_outcomes）在 PG 中的落地。
--
-- 记录所有已算出 direction + lim 的信号（无论最后是否实际下单）。
-- 策略 POST /signal 登记（幂等：重复调用 INSERT ON CONFLICT DO NOTHING）
-- 策略 POST /settlement 回填 winner → 服务端算 pnl_100 @100U 归一化
--   赢：pnl_100 = 100/lim - 100
--   输：pnl_100 = -100
-- 从此 slope 完全与实际仓位 size_usd 解耦，且被 slope gate 拦下的信号
-- 也有理论 PnL 推进窗口，破除"slope 永远 <0"的死锁。

CREATE TABLE IF NOT EXISTS slope_signal_outcomes (
    variant              TEXT        NOT NULL,
    signal_ts            TIMESTAMPTZ NOT NULL,    -- = 市场开盘 UTC ts（slug_ts）
    settle_ts            TIMESTAMPTZ NOT NULL,    -- = signal_ts + market_duration
    market_condition_id  TEXT,                    -- Polymarket condition_id，settlement 定位用
    market_slug          TEXT,
    direction            TEXT        NOT NULL,
    lim                  NUMERIC(6, 4) NOT NULL,  -- 0.5 < lim <= 1.0
    winner               TEXT,                    -- 'UP' / 'DOWN' / NULL（未结算）
    pnl_100              NUMERIC(8, 4),           -- @100U 归一化；未结算时 NULL
    source               TEXT NOT NULL DEFAULT 'live',  -- 'live' / 'backtest_bootstrap'
    recorded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at           TIMESTAMPTZ,             -- 回填 winner 的时间
    PRIMARY KEY (variant, signal_ts),
    CONSTRAINT slope_sig_direction_chk CHECK (direction IN ('UP', 'DOWN')),
    CONSTRAINT slope_sig_winner_chk    CHECK (winner IS NULL OR winner IN ('UP', 'DOWN')),
    CONSTRAINT slope_sig_lim_chk       CHECK (lim > 0.5 AND lim <= 1.0),
    CONSTRAINT slope_sig_variant_chk
        CHECK (variant IN ('btc_15m', 'btc_5m', 'btc_5m_ev', 'eth_15m', 'eth_5m'))
);

CREATE INDEX IF NOT EXISTS slope_sig_variant_settle_idx
    ON slope_signal_outcomes (variant, settle_ts DESC);

CREATE INDEX IF NOT EXISTS slope_sig_variant_unresolved_idx
    ON slope_signal_outcomes (variant, settle_ts DESC)
    WHERE winner IS NULL;

-- settlement 按 condition_id 定位（settler 天然用 condition_id）
CREATE INDEX IF NOT EXISTS slope_sig_condition_idx
    ON slope_signal_outcomes (market_condition_id)
    WHERE market_condition_id IS NOT NULL;

COMMENT ON TABLE slope_signal_outcomes IS
    'Slope Indicator Service 事实表：所有 direction+lim 已确定的信号，无论是否下单';


-- ── slope_cache（缓存）─────────────────────────────────────────────────────
-- 每 (variant, n_window) 一行，缓存最近一次计算结果。现在 cache 来源是
-- slope_signal_outcomes，不再是 median_trend_orders。

CREATE TABLE IF NOT EXISTS slope_cache (
    variant         TEXT        NOT NULL,
    n_window        INTEGER     NOT NULL,
    slope_value     NUMERIC(14, 6),
    allow_trade     BOOLEAN,
    n_in_window     INTEGER     NOT NULL DEFAULT 0,
    last_signal_ts  TIMESTAMPTZ,
    last_settle_ts  TIMESTAMPTZ,
    computed_ms     INTEGER,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (variant, n_window),
    CONSTRAINT slope_cache_variant_chk
        CHECK (variant IN ('btc_15m', 'btc_5m', 'btc_5m_ev', 'eth_15m', 'eth_5m')),
    CONSTRAINT slope_cache_n_window_chk
        CHECK (n_window > 0 AND n_window <= 500)
);

CREATE INDEX IF NOT EXISTS slope_cache_updated_at_idx
    ON slope_cache (updated_at DESC);


-- ── median_trend_risk_config 扩展：slope_n ─────────────────────────────────
-- 前端 RiskConfigCard 可配置的 N 值；NULL = 不启用 slope gate。

ALTER TABLE median_trend_risk_config
    ADD COLUMN IF NOT EXISTS slope_n INTEGER;

ALTER TABLE median_trend_risk_config
    DROP CONSTRAINT IF EXISTS median_trend_risk_config_slope_n_chk;

ALTER TABLE median_trend_risk_config
    ADD CONSTRAINT median_trend_risk_config_slope_n_chk
    CHECK (slope_n IS NULL OR (slope_n >= 5 AND slope_n <= 500));

COMMENT ON COLUMN median_trend_risk_config.slope_n IS
    'Slope gate 窗口大小；NULL=不启用';
