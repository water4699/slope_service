"""Slope Indicator Service CLI

Usage:
    python -m app.cli status
    python -m app.cli status --variant btc_5m --n 50
    python -m app.cli recompute
    python -m app.cli watch
    python -m app.cli bootstrap --variant btc_5m --limit 500
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass

from .service import SlopeService


def _build_dsn() -> str:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    db   = os.getenv("POSTGRES_DB",   "predictlab")
    user = os.getenv("POSTGRES_USER", "crawler")
    pwd  = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={db} user={user} password={pwd}"


def _active_variants(svc: SlopeService):
    import psycopg2
    with psycopg2.connect(svc._dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT variant, slope_n
                FROM median_trend_risk_config
                WHERE variant <> '__global__' AND slope_n IS NOT NULL
                ORDER BY variant
            """)
            return cur.fetchall()


def cmd_status(svc, variant, n):
    rows = [(variant, n)] if variant else _active_variants(svc)
    if not rows:
        print("(no variant with slope_n configured)")
        return
    print(f"{'variant':<12} {'N':>4} {'slope':>10} {'allow':>6} {'n/N':>10} {'ms':>5} {'last_settle_bj':<25}")
    for v, nw in rows:
        st = svc.get_status(v, nw)
        slope_s = f"{st.slope_value:.2f}" if st.slope_value is not None else "—"
        allow_s = "YES" if st.allow_trade else ("NO" if st.allow_trade is False else "—")
        last_s = st.last_settle_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if st.last_settle_ts else "—"
        print(f"{st.variant:<12} {st.n_window:>4} {slope_s:>10} {allow_s:>6} "
              f"{st.n_in_window}/{st.n_window:<6} {st.computed_ms or 0:>5} {last_s:<25}")


def cmd_recompute(svc):
    rows = svc.recompute_all()
    print(f"recomputed {len(rows)} variants:")
    for st in rows:
        slope_s = f"{st.slope_value:.2f}" if st.slope_value is not None else "—"
        print(f"  {st.variant:<12} N={st.n_window:<4} slope={slope_s:>10} n_in_window={st.n_in_window}")


def cmd_watch(svc, interval):
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            print(f"=== Slope Indicator Service watch (refresh every {interval}s) ===\n")
            cmd_status(svc, None, None)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n(stopped)")


def cmd_bootstrap(svc, variant: str, limit: int):
    """从 PredictLab 历史数据灌入 slope_signal_outcomes（用 limit_price 和
    市场最终 outcome）。

    数据源：median_trend_signals JOIN median_trend_orders（拿 settlement_outcome）
    只取 allowed=TRUE + dry_run=FALSE + settlement_outcome IN (UP,DOWN) 的。
    被 slope gate 拦下的历史信号不在 PredictLab 表里带 lim，无法 bootstrap。
    """
    import psycopg2
    count = 0
    skipped = 0
    with psycopg2.connect(svc._dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (s.id)
                       s.variant,
                       s.signal_ts,
                       s.market_condition_id,
                       s.market_slug,
                       s.direction,
                       s.limit_price::float AS lim_raw,
                       s.median_value::float AS median_value,
                       o.settlement_outcome AS winner
                FROM median_trend_signals s
                JOIN median_trend_orders o ON o.signal_id = s.id
                WHERE s.variant = %s
                  AND s.allowed = TRUE
                  AND s.dry_run = FALSE
                  AND s.direction IN ('UP','DOWN')
                  AND o.dry_run = FALSE
                  AND o.settlement_outcome IN ('UP','DOWN')
                ORDER BY s.id, o.id
                """,
                (variant,),
            )
            rows = cur.fetchall()
            # 按 signal_ts DESC 取 LIMIT，然后反转为时间正序插入
            rows.sort(key=lambda r: r[1], reverse=True)
            rows = rows[:limit][::-1]

            for v, signal_ts, cond, slug, direction, lim_raw, median_value, winner in rows:
                # 统一从 median_value + direction 重算 buy_lim，避免历史数据里
                # DOWN 方向 limit_price 可能存的是 median 而非 1-median 的旧版格式问题。
                # 退路：如果 median_value 为空，才 fallback 到 limit_price。
                buy_lim = None
                if median_value is not None:
                    mv = float(median_value)
                    if direction == "UP":
                        buy_lim = mv
                    else:  # DOWN
                        buy_lim = 1.0 - mv
                elif lim_raw is not None:
                    # limit_price 已经是买入价（新版存的）；DOWN 若 < 0.5 说明是旧版存了 median
                    raw = float(lim_raw)
                    if direction == "DOWN" and raw < 0.5:
                        buy_lim = 1.0 - raw
                    else:
                        buy_lim = raw
                if buy_lim is None:
                    skipped += 1
                    continue
                # clamp 到 (0.5, 1.0]；边界 0.5 也排除（pnl=100 刚好也没意义）
                if not (0.5 < buy_lim <= 1.0):
                    # 极小概率（median 刚好在边界附近），跳过这笔
                    skipped += 1
                    continue

                try:
                    ins = svc.record_signal(
                        variant=v, signal_ts=signal_ts,
                        direction=direction, lim=buy_lim,
                        market_condition_id=cond, market_slug=slug,
                        source="backtest_bootstrap",
                    )
                    if ins:
                        svc.record_settlement(
                            variant=v, signal_ts=signal_ts,
                            winner=winner,
                        )
                        count += 1
                except (ValueError, AssertionError) as exc:
                    print(f"  skip {v}@{signal_ts}: {exc}")
                    skipped += 1
    print(f"bootstrap: inserted {count} records for {variant} (skipped {skipped})")
    # 再重算一次 cache
    n = svc._load_config_n(variant)
    if n is not None:
        st = svc.get_status(variant, n)
        slope_s = f"{st.slope_value:.2f}" if st.slope_value is not None else "—"
        print(f"after bootstrap {variant} slope={slope_s} n_in_window={st.n_in_window}/{st.n_window}")


def main() -> None:
    p = argparse.ArgumentParser(description="Slope Indicator Service CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status")
    sp.add_argument("--variant", default=None)
    sp.add_argument("--n", type=int, default=None)

    sub.add_parser("recompute")

    sp = sub.add_parser("watch")
    sp.add_argument("--interval", type=float, default=30.0)

    sp = sub.add_parser("bootstrap",
        help="从 PredictLab 历史数据灌入 slope_signal_outcomes")
    sp.add_argument("--variant", required=True)
    sp.add_argument("--limit", type=int, default=500)

    args = p.parse_args()
    svc = SlopeService(_build_dsn(), default_warmup_allow=False)

    if args.cmd == "status":
        cmd_status(svc, args.variant, args.n)
    elif args.cmd == "recompute":
        cmd_recompute(svc)
    elif args.cmd == "watch":
        cmd_watch(svc, args.interval)
    elif args.cmd == "bootstrap":
        cmd_bootstrap(svc, args.variant, args.limit)


if __name__ == "__main__":
    main()
