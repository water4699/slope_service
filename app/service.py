"""Slope Indicator Service - 核心逻辑（按文档原方案）

关键：slope_signal_outcomes 是本服务的事实表。所有 direction+lim 已算出的信号
都登记进来（不管实际是否下单），这样被 slope gate 拦下的信号也能贡献理论
PnL 给 slope，破除 "slope 永远 <0" 的死锁。

PnL @100U 归一化：赢=100/lim-100，输=-100。固定公式，跟实际 size_usd 无关。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
import psycopg2
import psycopg2.extras


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
    slope_value: Optional[float]
    allow_trade: Optional[bool]
    n_in_window: int
    last_signal_ts: Optional[datetime]
    last_settle_ts: Optional[datetime]
    computed_ms: Optional[int]
    updated_at: Optional[datetime]
    warmup: bool


class SlopeService:

    def __init__(self, dsn: str, *, default_warmup_allow: bool = False):
        self._dsn = dsn
        self._default_warmup_allow = default_warmup_allow

    def _conn(self):
        return psycopg2.connect(self._dsn)

    # ─── 写入 API ────────────────────────────────────────────────────────────

    def record_signal(
        self,
        *,
        variant: str,
        signal_ts: datetime,
        direction: str,
        lim: float,
        market_condition_id: Optional[str] = None,
        market_slug: Optional[str] = None,
        source: str = "live",
    ) -> bool:
        """登记一条信号。幂等：重复调用不报错也不修改。

        约束（文档原版 assert）：
          * direction ∈ {UP, DOWN}
          * 0.5 < lim <= 1.0
          * variant 受 schema CHECK 约束
        """
        assert direction in ("UP", "DOWN"), f"invalid direction: {direction!r}"
        assert 0.5 < lim <= 1.0, f"invalid lim: {lim!r}"
        dur = DURATION.get(variant)
        if dur is None:
            raise ValueError(f"unknown variant: {variant}")

        settle_ts = signal_ts + _td(seconds=dur)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO slope_signal_outcomes
                        (variant, signal_ts, settle_ts,
                         market_condition_id, market_slug,
                         direction, lim, source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (variant, signal_ts) DO NOTHING
                    """,
                    (variant, signal_ts, settle_ts,
                     market_condition_id, market_slug,
                     direction, float(lim), source),
                )
                inserted = cur.rowcount > 0
            conn.commit()
        return inserted

    def record_settlement(
        self,
        *,
        variant: Optional[str] = None,
        signal_ts: Optional[datetime] = None,
        market_condition_id: Optional[str] = None,
        winner: str,
    ) -> int:
        """回填 winner，算 pnl_100，更新 cache。

        定位方式二选一：
          (variant, signal_ts)           — 最精确
          market_condition_id            — 按 condition_id（settler 天然有）

        返回：affected_variants 数（用于上层日志）
        """
        assert winner in ("UP", "DOWN"), f"invalid winner: {winner!r}"
        affected: List[str] = []

        with self._conn() as conn:
            with conn.cursor() as cur:
                if variant is not None and signal_ts is not None:
                    cur.execute(
                        """
                        UPDATE slope_signal_outcomes
                           SET winner = %s,
                               pnl_100 = CASE WHEN direction = %s
                                              THEN ROUND((100.0 / lim - 100.0)::numeric, 4)
                                              ELSE -100.0 END,
                               settled_at = now()
                         WHERE variant = %s
                           AND signal_ts = %s
                           AND winner IS NULL
                         RETURNING variant
                        """,
                        (winner, winner, variant, signal_ts),
                    )
                elif market_condition_id is not None:
                    cur.execute(
                        """
                        UPDATE slope_signal_outcomes
                           SET winner = %s,
                               pnl_100 = CASE WHEN direction = %s
                                              THEN ROUND((100.0 / lim - 100.0)::numeric, 4)
                                              ELSE -100.0 END,
                               settled_at = now()
                         WHERE market_condition_id = %s
                           AND winner IS NULL
                         RETURNING variant
                        """,
                        (winner, winner, market_condition_id),
                    )
                else:
                    raise ValueError(
                        "record_settlement requires either (variant, signal_ts) "
                        "or market_condition_id"
                    )
                affected = [r[0] for r in cur.fetchall()]
            conn.commit()

        # 被更新的 variant 立刻重算 cache
        for v in set(affected):
            n = self._load_config_n(v)
            if n is not None:
                self._recompute(v, n)
        return len(affected)

    # ─── 读取 API ────────────────────────────────────────────────────────────

    def allow_trade(self, variant: str, n_window: Optional[int] = None) -> bool:
        cfg_n = self._load_config_n(variant)
        if n_window is None:
            n_window = cfg_n
        if n_window is None:
            return True  # slope_n 未配置 = gate 未启用
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
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT variant, slope_n
                    FROM median_trend_risk_config
                    WHERE variant <> '__global__' AND slope_n IS NOT NULL
                """)
                rows = cur.fetchall()
        return [self._recompute(v, int(n)) for v, n in rows]

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
        t0 = time.time()
        with self._conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.NamedTupleCursor) as cur:
                # 最近 n_window 笔已结算信号（winner IS NOT NULL）
                cur.execute(
                    """
                    SELECT signal_ts, settle_ts, direction, lim, winner, pnl_100
                    FROM slope_signal_outcomes
                    WHERE variant = %s
                      AND winner IS NOT NULL
                      AND pnl_100 IS NOT NULL
                    ORDER BY settle_ts DESC
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
        last_settle_ts = rows[0].settle_ts if rows else None

        if not warmup:
            asc = rows[::-1]
            ts_seconds = np.array(
                [r.settle_ts.timestamp() for r in asc], dtype=np.float64
            )
            xs = (ts_seconds - ts_seconds[0]) / 3600.0
            ys = np.cumsum([float(r.pnl_100) for r in asc])
            slope, _ = np.polyfit(xs, ys, 1)
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


# tiny helper，避免顶部再 import timedelta
def _td(**kw):
    from datetime import timedelta
    return timedelta(**kw)
