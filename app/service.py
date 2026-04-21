"""Slope Indicator Service - 核心计算逻辑

不维护自己的 signal 事实表，直接从 PredictLab 的 median_trend_signals +
median_trend_orders 读已结算信号，按 @100U 归一化 PnL 后累计回归算斜率。

关键约束（方案 A）：
  * 只纳入 allowed=TRUE 且已结算的信号（settlement_outcome IN ('UP','DOWN')）
  * 同一 signal 可能有多个账号的订单，取一条即可（direction 相同）
  * 忽略 cancelled / failed / risk_blocked
  * 忽略 dry_run
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import psycopg2
import psycopg2.extras


# 每 variant 的市场时长（秒），用于诊断输出
DURATION: Dict[str, int] = {
    "btc_5m": 300,
    "btc_5m_ev": 300,
    "eth_5m": 300,
    "btc_15m": 900,
    "eth_15m": 900,
}


@dataclass
class SlopeStatus:
    variant: str
    n_window: int
    slope_value: Optional[float]          # $/hr；warmup 期 None
    allow_trade: Optional[bool]           # warmup 期 None
    n_in_window: int
    last_signal_ts: Optional[datetime]
    last_settle_ts: Optional[datetime]
    computed_ms: Optional[int]
    updated_at: Optional[datetime]
    warmup: bool                          # n_in_window < n_window


class SlopeService:
    """基于 PG 现有表的斜率服务。所有接口都是幂等只读 + cache upsert。"""

    def __init__(self, dsn: str, *, default_warmup_allow: bool = False):
        """
        dsn: psycopg2 DSN（含 host/user/password/dbname）
        default_warmup_allow: warmup 期 / 数据不足时 allow_trade 的默认值
            * 我们固定 fail-close = False（用户 2026-04-21 决定）
            * 但仍接受构造器参数以便测试覆盖
        """
        self._dsn = dsn
        self._default_warmup_allow = default_warmup_allow

    def _conn(self):
        return psycopg2.connect(self._dsn)

    # ─── 公开 API ────────────────────────────────────────────────────────────

    def allow_trade(self, variant: str, n_window: Optional[int] = None) -> bool:
        """下单决策闸门。

        n_window=None 时从 median_trend_risk_config.slope_n 读（前端控制）。
        slope_n IS NULL → slope gate 未启用，永远返回 True。
        """
        cfg_n = self._load_config_n(variant)
        if n_window is None:
            n_window = cfg_n
        if n_window is None:
            return True  # 未启用

        st = self._recompute(variant, n_window)
        if st.warmup:
            return self._default_warmup_allow
        return bool(st.allow_trade)

    def get_status(self, variant: str, n_window: Optional[int] = None) -> SlopeStatus:
        cfg_n = self._load_config_n(variant)
        if n_window is None:
            n_window = cfg_n
        if n_window is None:
            return SlopeStatus(
                variant=variant, n_window=0,
                slope_value=None, allow_trade=None, n_in_window=0,
                last_signal_ts=None, last_settle_ts=None,
                computed_ms=None, updated_at=None, warmup=True,
            )
        return self._recompute(variant, n_window)

    def recompute_all(self) -> List[SlopeStatus]:
        """遍历 median_trend_risk_config 里 slope_n 非 NULL 的 variant 全部重算。"""
        out: List[SlopeStatus] = []
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT variant, slope_n
                    FROM median_trend_risk_config
                    WHERE variant <> '__global__' AND slope_n IS NOT NULL
                """)
                rows = cur.fetchall()
        for variant, n in rows:
            out.append(self._recompute(variant, int(n)))
        return out

    # ─── 内部 ───────────────────────────────────────────────────────────────

    def _load_config_n(self, variant: str) -> Optional[int]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slope_n FROM median_trend_risk_config WHERE variant = %s",
                    (variant,),
                )
                row = cur.fetchone()
                if not row or row[0] is None:
                    return None
                return int(row[0])

    def _recompute(self, variant: str, n_window: int) -> SlopeStatus:
        """查最近 n_window 笔已结算信号，算 slope，upsert cache。"""
        t0 = time.time()
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.NamedTupleCursor) as cur:
                # 每个 signal 取一条订单的 settlement_outcome（DISTINCT ON 去重）
                cur.execute(
                    """
                    SELECT signal_ts, direction, lim, winner, pnl_100
                    FROM (
                        SELECT DISTINCT ON (s.id)
                               s.signal_ts,
                               s.direction,
                               s.limit_price::float AS lim,
                               o.settlement_outcome AS winner,
                               CASE
                                 WHEN s.direction = o.settlement_outcome
                                   THEN 100.0 / s.limit_price - 100.0
                                 ELSE -100.0
                               END AS pnl_100
                        FROM median_trend_signals s
                        JOIN median_trend_orders o ON o.signal_id = s.id
                        WHERE s.variant = %s
                          AND s.allowed = TRUE
                          AND s.dry_run = FALSE
                          AND s.limit_price IS NOT NULL
                          AND s.direction IN ('UP', 'DOWN')
                          AND o.dry_run = FALSE
                          AND o.settlement_outcome IN ('UP', 'DOWN')
                        ORDER BY s.id, o.id
                    ) q
                    ORDER BY signal_ts DESC
                    LIMIT %s
                    """,
                    (variant, n_window),
                )
                rows = cur.fetchall()

        computed_ms = int((time.time() - t0) * 1000)

        n_in_window = len(rows)
        warmup = n_in_window < n_window

        slope_value: Optional[float] = None
        allow_trade: Optional[bool] = None
        last_signal_ts = rows[0].signal_ts if rows else None
        last_settle_ts = last_signal_ts  # settlement_ts 约等于 signal_ts + market_duration，这里简化取 signal_ts

        if not warmup:
            # 按时间升序反转；x 用小时为单位（避免大数）
            asc = rows[::-1]
            ts_seconds = np.array(
                [r.signal_ts.timestamp() for r in asc], dtype=np.float64
            )
            xs = (ts_seconds - ts_seconds[0]) / 3600.0
            ys = np.cumsum([float(r.pnl_100) for r in asc])
            slope, _intercept = np.polyfit(xs, ys, 1)
            slope_value = float(slope)
            allow_trade = slope_value >= 0

        # upsert cache
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO slope_cache
                        (variant, n_window, slope_value, allow_trade,
                         n_in_window, last_signal_ts, last_settle_ts,
                         computed_ms, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (variant, n_window) DO UPDATE SET
                        slope_value    = EXCLUDED.slope_value,
                        allow_trade    = EXCLUDED.allow_trade,
                        n_in_window    = EXCLUDED.n_in_window,
                        last_signal_ts = EXCLUDED.last_signal_ts,
                        last_settle_ts = EXCLUDED.last_settle_ts,
                        computed_ms    = EXCLUDED.computed_ms,
                        updated_at     = now()
                    """,
                    (variant, n_window, slope_value, allow_trade,
                     n_in_window, last_signal_ts, last_settle_ts, computed_ms),
                )
            conn.commit()

        return SlopeStatus(
            variant=variant, n_window=n_window,
            slope_value=slope_value, allow_trade=allow_trade,
            n_in_window=n_in_window,
            last_signal_ts=last_signal_ts, last_settle_ts=last_settle_ts,
            computed_ms=computed_ms,
            updated_at=datetime.now(tz=timezone.utc),
            warmup=warmup,
        )
