-- ============================================================================
-- Slope Indicator Service - DB schema
--
-- 本服务复用 PredictLab 的 Postgres 数据库（database=predictlab），
-- 只读 median_trend_signals + median_trend_orders，写 slope_cache。
--
-- 执行：
--   psql -h 127.0.0.1 -U crawler -d predictlab -f db/schema.sql
-- ============================================================================


-- ── slope_cache ────────────────────────────────────────────────────────────
-- 每 (variant, n_window) 一行，缓存最近一次计算结果。
-- allow_trade 表示调用方下单决策（slope >= 0 = 1，否则 0）。
-- n_in_window < n_window 时 warmup=TRUE，slope_value 可以为 NULL。

CREATE TABLE IF NOT EXISTS slope_cache (
    variant         TEXT        NOT NULL,
    n_window        INTEGER     NOT NULL,
    slope_value     NUMERIC(14, 6),       -- $/hr 斜率；warmup 时 NULL
    allow_trade     BOOLEAN,              -- slope >= 0 真为 TRUE；warmup 时 NULL
    n_in_window     INTEGER     NOT NULL DEFAULT 0,
    last_signal_ts  TIMESTAMPTZ,          -- 窗口内最新一笔信号时间
    last_settle_ts  TIMESTAMPTZ,          -- 窗口内最新一笔结算时间
    computed_ms     INTEGER,              -- 本次计算耗时（毫秒），运维观测用
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (variant, n_window),
    CONSTRAINT slope_cache_variant_chk
        CHECK (variant IN ('btc_15m', 'btc_5m', 'btc_5m_ev', 'eth_15m', 'eth_5m')),
    CONSTRAINT slope_cache_n_window_chk
        CHECK (n_window > 0 AND n_window <= 500)
);

CREATE INDEX IF NOT EXISTS slope_cache_updated_at_idx
    ON slope_cache (updated_at DESC);

COMMENT ON TABLE slope_cache IS
    'Slope Indicator Service - 每 (variant, N) 缓存的最新斜率 / allow_trade 决策';


-- ── median_trend_risk_config 扩展：slope_n ─────────────────────────────────
-- 前端 RiskConfigCard 可配置的 N 值；NULL = 不启用 slope gate。
-- 本列已由 slope service 模块读取，PredictLab 引擎侧也会读。

ALTER TABLE median_trend_risk_config
    ADD COLUMN IF NOT EXISTS slope_n INTEGER;

ALTER TABLE median_trend_risk_config
    DROP CONSTRAINT IF EXISTS median_trend_risk_config_slope_n_chk;

ALTER TABLE median_trend_risk_config
    ADD CONSTRAINT median_trend_risk_config_slope_n_chk
    CHECK (slope_n IS NULL OR (slope_n >= 5 AND slope_n <= 500));

COMMENT ON COLUMN median_trend_risk_config.slope_n IS
    'Slope gate 窗口大小：最近 N 笔已结算信号斜率 <0 则拦截新信号；NULL=不启用';
