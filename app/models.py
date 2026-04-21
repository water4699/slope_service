"""HTTP 层 pydantic schemas"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ─── 写入端 ────────────────────────────────────────────────────────────────

class SignalPostBody(BaseModel):
    variant: str
    signal_ts: datetime                 # ISO8601 / epoch-number 都行（pydantic 会解析）
    direction: str                      # 'UP' / 'DOWN'
    lim: float = Field(..., gt=0.5, le=1.0)
    market_condition_id: Optional[str] = None
    market_slug: Optional[str] = None
    source: str = "live"


class SignalPostResponse(BaseModel):
    ok: bool = True
    inserted: bool                      # False = 已存在（幂等）


class SettlementPostBody(BaseModel):
    # 二选一定位键：优先 (variant, signal_ts)；否则 market_condition_id
    variant: Optional[str] = None
    signal_ts: Optional[datetime] = None
    market_condition_id: Optional[str] = None
    winner: str                          # 'UP' / 'DOWN'


class SettlementPostResponse(BaseModel):
    ok: bool = True
    affected: int                        # 被回填的行数


# ─── 读取端 ────────────────────────────────────────────────────────────────

class AllowResponse(BaseModel):
    variant: str
    allow_trade: bool
    n_window: Optional[int] = None
    n_in_window: int = 0
    slope_value: Optional[float] = None
    warmup: bool = False
    enabled: bool = True
    reason: Optional[str] = None


class StatusResponse(BaseModel):
    variant: str
    n_window: int
    slope_value: Optional[float] = None
    allow_trade: Optional[bool] = None
    n_in_window: int
    last_signal_ts: Optional[datetime] = None
    last_settle_ts: Optional[datetime] = None
    computed_ms: Optional[int] = None
    updated_at: Optional[datetime] = None
    warmup: bool


class RecomputeResponse(BaseModel):
    ok: bool = True
    computed: List[StatusResponse] = Field(default_factory=list)


class HealthResponse(BaseModel):
    ok: bool = True
    service: str = "slope-indicator"
    db_reachable: bool
