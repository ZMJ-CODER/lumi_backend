from app.agents.orchestration.planning.confidence_calibration import (
    apply_temperature,
    expected_calibration_error,
    fit_temperature,
)


def test_temperature_scaling_is_monotonic_and_bounded():
    assert 0 < apply_temperature(0.8, 2.0) < 0.8
    assert 0 < apply_temperature(0.2, 2.0) < 0.5


def test_calibration_metrics_are_reproducible():
    confidences = [0.9, 0.8, 0.2, 0.1]
    labels = [True, True, False, False]
    result = fit_temperature(confidences, labels)
    assert result.sample_count == 4
    assert result.after_brier <= result.before_brier
    assert expected_calibration_error(confidences, labels) >= 0
