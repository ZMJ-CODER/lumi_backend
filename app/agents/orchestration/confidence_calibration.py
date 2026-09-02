"""无供应商依赖的路由置信度校准工具。

L3 当前只能提供 ``confidence_hint``。本模块不把该提示伪装成概率，而是
为带真实标签的离线样本提供温度搜索、ECE 与 Brier 统计，供策略发布前使用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


def _clip(value: float) -> float:
    return max(1e-6, min(1.0 - 1e-6, float(value)))


def _logit(probability: float) -> float:
    p = _clip(probability)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def apply_temperature(confidence: float, temperature: float) -> float:
    """Apply binary temperature scaling to a model confidence hint."""
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    return _sigmoid(_logit(confidence) / float(temperature))


def brier_score(confidences: Iterable[float], labels: Iterable[bool | int]) -> float:
    pairs = [(_clip(p), 1.0 if bool(label) else 0.0) for p, label in zip(confidences, labels, strict=False)]
    if not pairs:
        return 0.0
    return sum((p - label) ** 2 for p, label in pairs) / len(pairs)


def expected_calibration_error(
    confidences: Iterable[float],
    labels: Iterable[bool | int],
    *,
    bins: int = 10,
) -> float:
    """Return equal-width binary ECE for a labelled evaluation slice."""
    if bins <= 0:
        raise ValueError("bins must be positive")
    grouped: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for confidence, label in zip(confidences, labels, strict=False):
        probability = _clip(confidence)
        index = min(bins - 1, int(probability * bins))
        grouped[index].append((probability, 1.0 if bool(label) else 0.0))
    total = sum(len(group) for group in grouped)
    if not total:
        return 0.0
    return sum(
        len(group) / total * abs(
            sum(probability for probability, _ in group) / len(group)
            - sum(label for _, label in group) / len(group)
        )
        for group in grouped if group
    )


@dataclass(frozen=True)
class CalibrationResult:
    temperature: float
    before_ece: float
    after_ece: float
    before_brier: float
    after_brier: float
    sample_count: int


def fit_temperature(
    confidences: Iterable[float],
    labels: Iterable[bool | int],
    *,
    bins: int = 10,
) -> CalibrationResult:
    """Fit a conservative temperature by deterministic grid search.

    A small labelled set is not enough to justify a learned calibrator; the
    bounded grid is reproducible and falls back to 1.0 when no samples exist.
    """
    probabilities = [_clip(value) for value in confidences]
    targets = [bool(value) for value in labels]
    if len(probabilities) != len(targets):
        raise ValueError("confidences and labels must have equal length")
    before_ece = expected_calibration_error(probabilities, targets, bins=bins)
    before_brier = brier_score(probabilities, targets)
    best_temperature = 1.0
    best_loss = before_brier
    for step in range(1, 81):
        temperature = 0.25 + step * 0.05
        calibrated = [apply_temperature(value, temperature) for value in probabilities]
        loss = brier_score(calibrated, targets)
        if loss < best_loss - 1e-12:
            best_loss = loss
            best_temperature = temperature
    calibrated = [apply_temperature(value, best_temperature) for value in probabilities]
    return CalibrationResult(
        temperature=best_temperature,
        before_ece=before_ece,
        after_ece=expected_calibration_error(calibrated, targets, bins=bins),
        before_brier=before_brier,
        after_brier=brier_score(calibrated, targets),
        sample_count=len(probabilities),
    )
