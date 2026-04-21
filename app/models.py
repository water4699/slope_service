"""HTTP 层 pydantic schemas"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class AllowResponse(BaseModel):
    variant: str
    allow_trade: bool
    n_window: Optional[int] = None
    n_in_window: int = 0
    slope_value: Optional[float] = None
    warmup: bool = False
    enabled: bool = True                  # slope_n IS NULL → enabled=False
    reason: Optional[str] = None          # 诊断用："enabled=false" / "warmup" / "slope<0" / "ok"


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
