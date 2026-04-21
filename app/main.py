"""FastAPI 入口

端点：
  GET  /health                              健康检查（含 DB 连通）
  GET  /allow/{variant}?n=<N>               下单决策；n 留空则读 median_trend_risk_config.slope_n
  GET  /status?variant=<V>&n=<N>            状态诊断
  POST /recompute                           遍历所有 slope_n 非 NULL 的 variant 强制重算

认证：
  非公开读端点 (POST /recompute) 必须带 X-Slope-Token
  GET /allow 和 GET /status 默认无需 token（同机 127.0.0.1 调用）
  如需加固，设 SLOPE_REQUIRE_TOKEN=1 后所有端点都要 token
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from .models import (
    AllowResponse,
    HealthResponse,
    RecomputeResponse,
    StatusResponse,
)
from .service import SlopeService


logger = logging.getLogger("slope_service")
logging.basicConfig(
    level=os.getenv("SLOPE_LOG_LEVEL", "INFO").upper(),
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


# ─── 配置 ────────────────────────────────────────────────────────────────────

def _build_dsn() -> str:
    """从环境变量拼 psycopg2 DSN。默认指向 PredictLab 同机 PG。"""
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    db   = os.getenv("POSTGRES_DB",   "predictlab")
    user = os.getenv("POSTGRES_USER", "crawler")
    pwd  = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={db} user={user} password={pwd}"


SLOPE_TOKEN = os.getenv("SLOPE_TOKEN", "")
SLOPE_REQUIRE_TOKEN_ALL = os.getenv("SLOPE_REQUIRE_TOKEN", "0") == "1"


def _require_token(x_slope_token: Optional[str] = Header(None)) -> None:
    """POST /recompute 等写端点必须带 token。"""
    if not SLOPE_TOKEN:
        return  # 未配置 token 则放行（本地开发）
    if x_slope_token != SLOPE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid slope token")


def _optional_token(x_slope_token: Optional[str] = Header(None)) -> None:
    """GET 端点的可选认证。SLOPE_REQUIRE_TOKEN=1 时必须带。"""
    if not SLOPE_REQUIRE_TOKEN_ALL:
        return
    _require_token(x_slope_token)


# ─── 应用 ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Slope Indicator Service",
    version="0.1.0",
    description="Polymarket 策略 PnL 斜率熔断闸门服务",
)


def _service() -> SlopeService:
    # 全局单例；psycopg2 连接是每次 query 新建，不缓存连接对象
    return SlopeService(_build_dsn(), default_warmup_allow=False)


# ─── 端点 ────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    db_ok = True
    try:
        import psycopg2
        with psycopg2.connect(_build_dsn(), connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except Exception as exc:
        logger.warning(f"db health check failed: {exc}")
        db_ok = False
    return HealthResponse(ok=db_ok, db_reachable=db_ok)


@app.get("/allow/{variant}", response_model=AllowResponse,
         dependencies=[Depends(_optional_token)])
def allow_trade(
    variant: str,
    n: Optional[int] = Query(default=None, ge=5, le=500),
) -> AllowResponse:
    svc = _service()
    st = svc.get_status(variant, n)

    if st.n_window == 0:
        return AllowResponse(
            variant=variant, allow_trade=True, n_window=None,
            n_in_window=0, enabled=False, reason="slope_gate_disabled",
        )

    if st.warmup:
        return AllowResponse(
            variant=variant, allow_trade=False, n_window=st.n_window,
            n_in_window=st.n_in_window, slope_value=None,
            warmup=True, enabled=True,
            reason=f"warmup({st.n_in_window}/{st.n_window})",
        )

    allow = bool(st.allow_trade)
    return AllowResponse(
        variant=variant, allow_trade=allow, n_window=st.n_window,
        n_in_window=st.n_in_window, slope_value=st.slope_value,
        warmup=False, enabled=True,
        reason="ok" if allow else f"slope<0({st.slope_value:.2f})",
    )


@app.get("/status", response_model=StatusResponse,
         dependencies=[Depends(_optional_token)])
def status(
    variant: str = Query(...),
    n: Optional[int] = Query(default=None, ge=5, le=500),
) -> StatusResponse:
    svc = _service()
    st = svc.get_status(variant, n)
    return StatusResponse(
        variant=st.variant, n_window=st.n_window,
        slope_value=st.slope_value, allow_trade=st.allow_trade,
        n_in_window=st.n_in_window,
        last_signal_ts=st.last_signal_ts, last_settle_ts=st.last_settle_ts,
        computed_ms=st.computed_ms, updated_at=st.updated_at,
        warmup=st.warmup,
    )


@app.post("/recompute", response_model=RecomputeResponse,
          dependencies=[Depends(_require_token)])
def recompute() -> RecomputeResponse:
    svc = _service()
    out = svc.recompute_all()
    return RecomputeResponse(
        ok=True,
        computed=[
            StatusResponse(
                variant=s.variant, n_window=s.n_window,
                slope_value=s.slope_value, allow_trade=s.allow_trade,
                n_in_window=s.n_in_window,
                last_signal_ts=s.last_signal_ts, last_settle_ts=s.last_settle_ts,
                computed_ms=s.computed_ms, updated_at=s.updated_at,
                warmup=s.warmup,
            ) for s in out
        ],
    )
