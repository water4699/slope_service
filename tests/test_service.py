"""Slope Service 基础测试

说明：SlopeService 依赖 PG + PredictLab 的 median_trend 表。
本测试只覆盖「不需要 DB 的纯逻辑」部分。完整端到端测试请用 docker-compose 起 PG 后手跑。
"""
from __future__ import annotations

import numpy as np
import pytest


def _compute_slope(pnl_series: list[float], step_seconds: int = 300) -> float:
    """复刻 SlopeService._recompute 里 numpy.polyfit 的核心逻辑。"""
    ts = np.arange(len(pnl_series), dtype=np.float64) * step_seconds
    xs = (ts - ts[0]) / 3600.0
    ys = np.cumsum(pnl_series)
    slope, _ = np.polyfit(xs, ys, 1)
    return float(slope)


def test_all_win_positive_slope():
    """全部赢单 → 斜率明显 > 0"""
    # lim=0.75，赢则 +33.33，输则 -100
    wins = [100.0 / 0.75 - 100.0] * 20
    slope = _compute_slope(wins, step_seconds=300)
    assert slope > 0, f"expected positive, got {slope}"


def test_all_loss_negative_slope():
    """全部输单 → 斜率明显 < 0"""
    losses = [-100.0] * 20
    slope = _compute_slope(losses, step_seconds=300)
    assert slope < 0, f"expected negative, got {slope}"


def test_flat_sequence_zero_slope():
    """PnL 0 序列 → slope ≈ 0"""
    flat = [0.0] * 20
    slope = _compute_slope(flat, step_seconds=300)
    assert abs(slope) < 1e-6


def test_recent_losses_flip_sign():
    """赢 10 笔 + 输 10 笔 → 窗口尾部累计下滑，slope 应转负"""
    wins = [100.0 / 0.75 - 100.0] * 10
    losses = [-100.0] * 10
    slope = _compute_slope(wins + losses, step_seconds=300)
    # 累计 pnl 先升后降，回归线斜率因为尾部跌得更猛应为负
    assert slope < 0, f"expected negative after reversal, got {slope}"


def test_lim_pnl_formula():
    """归一化 PnL 公式：赢 = 100/lim - 100；输 = -100"""
    # lim=0.60 → 赢 +66.67；lim=0.90 → 赢 +11.11
    assert round(100.0 / 0.60 - 100.0, 2) == 66.67
    assert round(100.0 / 0.90 - 100.0, 2) == 11.11


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
