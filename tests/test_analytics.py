import pytest
from app.analytics import analyze, evaluate


def candles(direction=0, interval=900_000, end=180_000_000):
    result = []
    for i in range(120):
        price = 100 + direction * i * .2
        start = end - (120 - i) * interval
        result.append([start, str(price), str(price + .1), str(price - .1), str(price), "10", start + interval - 1])
    return result


@pytest.mark.parametrize('slope,expected', [(1, 'UP'), (-1, 'DOWN'), (0, 'WAIT')])
def test_monotonic_and_flat_markets(slope, expected):
    result = analyze(candles(slope), 1)
    assert result['direction'] == expected
    assert 0 <= result['confidence'] <= 100
    assert result['neutral_pct'] >= .12
    if slope == 0:
        assert result['confidence'] == 0
        assert result['indicators']['rsi14'] == 50


def test_missing_candle_rejects_forecast():
    data = candles(1)
    del data[-30]
    with pytest.raises(ValueError, match='Пропуск'):
        analyze(data, 1)


@pytest.mark.parametrize('direction,actual,correct,observed', [
    ('UP', 101, True, 'UP'), ('UP', 99, False, 'DOWN'), ('DOWN', 99, True, 'DOWN'),
    ('WAIT', 100.05, True, 'WAIT'), ('UP', 100.05, False, 'WAIT'), ('WAIT', 101, False, 'UP')])
def test_scoring_respects_fixed_neutral_zone(direction, actual, correct, observed):
    result = evaluate(direction, 100, actual, .12)
    assert result['correct'] is correct
    assert result['observed_direction'] == observed


def test_fixed_window_reproducible():
    data = candles(1)
    assert analyze(data, 1) == analyze(data[-100:], 1)
