"""Slope Indicator Service CLI

Usage:
    python -m app.cli status                  # 遍历所有已启用 variant
    python -m app.cli status --variant btc_5m --n 50
    python -m app.cli recompute               # 强制重算所有
    python -m app.cli watch                   # 30s 轮询监控面板
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from .service import SlopeService


def _build_dsn() -> str:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    db   = os.getenv("POSTGRES_DB",   "predictlab")
    user = os.getenv("POSTGRES_USER", "crawler")
    pwd  = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={db} user={user} password={pwd}"


def _active_variants(svc: SlopeService):
    """从 median_trend_risk_config 拉出 slope_n 非 NULL 的 variant。"""
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


def cmd_status(svc: SlopeService, variant: Optional[str], n: Optional[int]) -> None:
    rows = [(variant, n)] if variant else _active_variants(svc)
    if not rows:
        print("(no variant with slope_n configured)")
        return
    print(f"{'variant':<12} {'N':>4} {'slope':>10} {'allow':>6} {'n/N':>8} {'ms':>5} {'last_signal_bj':<25}")
    for v, nw in rows:
        st = svc.get_status(v, nw)
        slope_s = f"{st.slope_value:.2f}" if st.slope_value is not None else "—"
        allow_s = "YES" if st.allow_trade else ("NO" if st.allow_trade is False else "—")
        last_s = st.last_signal_ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if st.last_signal_ts else "—"
        print(f"{st.variant:<12} {st.n_window:>4} {slope_s:>10} {allow_s:>6} "
              f"{st.n_in_window}/{st.n_window:<4} {st.computed_ms or 0:>5} {last_s:<25}")


def cmd_recompute(svc: SlopeService) -> None:
    rows = svc.recompute_all()
    print(f"recomputed {len(rows)} variants:")
    for st in rows:
        slope_s = f"{st.slope_value:.2f}" if st.slope_value is not None else "—"
        print(f"  {st.variant:<12} N={st.n_window:<4} slope={slope_s:>10} "
              f"n_in_window={st.n_in_window}")


def cmd_watch(svc: SlopeService, interval: float) -> None:
    try:
        while True:
            # 清屏 + 回到顶
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            print(f"=== Slope Indicator Service watch (refresh every {interval}s) ===\n")
            cmd_status(svc, None, None)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n(stopped)")


def main() -> None:
    p = argparse.ArgumentParser(description="Slope Indicator Service CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="print slope status")
    sp.add_argument("--variant", default=None)
    sp.add_argument("--n", type=int, default=None)

    sub.add_parser("recompute", help="force recompute all active variants")

    sp = sub.add_parser("watch", help="monitor panel (Ctrl+C to quit)")
    sp.add_argument("--interval", type=float, default=30.0)

    args = p.parse_args()
    svc = SlopeService(_build_dsn(), default_warmup_allow=False)

    if args.cmd == "status":
        cmd_status(svc, args.variant, args.n)
    elif args.cmd == "recompute":
        cmd_recompute(svc)
    elif args.cmd == "watch":
        cmd_watch(svc, args.interval)


if __name__ == "__main__":
    main()
