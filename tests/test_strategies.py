"""Deterministic synthetic cases, not a historical backtest or parameter search."""
import copy
import json
import math
import unittest
from contextlib import contextmanager

from app import strategies as s


END = 1_500 * s.HOUR_MS
NOW = END + 10_000


def candle(start, close=1000., width=4.):
    return {"time": start, "open": close, "high": close + width,
            "low": close - width, "close": close, "volume": 10.,
            "close_time": start + s.HOUR_MS - 1}


def history(count=500, slope=0., base=1000., width=4.):
    return [candle(END - (count - i) * s.HOUR_MS, base + slope * i, width)
            for i in range(count)]


def run(rows, **kwargs):
    return s.analyze_strategies(rows, now=kwargs.get("now", NOW),
                                confirmed_at=kwargs.get("confirmed_at", NOW))


@contextmanager
def fixed_emas(ema20, ema50):
    """Isolate exact decision boundaries from EMA warm-up arithmetic."""
    original = s.ema
    s.ema = lambda values, period: {20: ema20, 50: ema50}[period]
    try:
        yield
    finally:
        s.ema = original


def vote(identity, direction="UP", status="OK", score=1.):
    return {"strategy_id": identity, "strategy_version": "v1", "status": status,
            "direction": direction, "score": score, "input_close_at": END,
            "base_price": 1000., "due_at": {h: END + h * s.HOUR_MS for h in (1, 4)},
            "thresholds_pct": {1: 0.10, 4: 0.20}}


class InputTests(unittest.TestCase):
    def test_499_is_unavailable_and_500_is_enough(self):
        for count in (0, 499, 500):
            with self.subTest(count=count):
                result = run(history(count))
                for key in ("A", "B"):
                    self.assertEqual(result[key]["status"], "OK" if count == 500 else "DATA_UNAVAILABLE")
                    if count < 500:
                        self.assertIsNone(result[key]["direction"])
                        self.assertIsNone(result[key]["score"])
                        self.assertIn("INSUFFICIENT_HISTORY", result[key]["reason_codes"])

    def test_closed_window_excludes_forming_and_future_prices(self):
        rows = history(slope=2., width=.25)
        expected = run(rows)
        forming = candle(END, close=1e100)
        future = candle(END + s.HOUR_MS, close=1e200)
        self.assertEqual(run(rows + [forming, future]), expected)
        # Even invalid future OHLCV must never enter the indicators.
        forming["close"] = float("nan")
        self.assertEqual(run(rows + [forming]), expected)

    def test_forming_candle_cannot_supply_500th_warmup_bar(self):
        result = run(history(499) + [candle(END)])
        self.assertEqual(result["A"]["status"], "DATA_UNAVAILABLE")

    def test_confirmation_before_close_or_from_future_is_rejected(self):
        for confirmation in (None, END - 1, NOW + 1):
            with self.subTest(confirmation=confirmation):
                result = run(history(), confirmed_at=confirmation)
                self.assertIn("CLOSE_NOT_CONFIRMED", result["A"]["reason_codes"])

    def test_exact_close_boundary_is_allowed(self):
        self.assertEqual(run(history(), now=END, confirmed_at=END)["A"]["status"], "OK")

    def test_stale_last_closed_candle_is_rejected(self):
        result = run(history(), now=NOW + s.HOUR_MS, confirmed_at=NOW + s.HOUR_MS)
        self.assertIn("STALE_LAST_CANDLE", result["A"]["reason_codes"])

    def test_gap_duplicate_and_out_of_order_are_rejected(self):
        for kind in ("gap", "duplicate", "order"):
            rows = history(501)
            if kind == "gap":
                del rows[-50]
            elif kind == "duplicate":
                rows[-50] = dict(rows[-51])
            else:
                rows[-50], rows[-51] = rows[-51], rows[-50]
            with self.subTest(kind=kind):
                self.assertIn("NON_CONTIGUOUS_HISTORY", run(rows)["A"]["reason_codes"])

    def test_invalid_ohlcv_and_non_hourly_data_are_rejected(self):
        changes = ({"close": float("nan")}, {"high": float("inf")},
                   {"close": 0.}, {"volume": -1.}, {"high": 999.},
                   {"close_time": END - 2}, {"time": END - s.HOUR_MS + 1})
        for change in changes:
            rows = history()
            rows[-1].update(change)
            with self.subTest(change=change):
                self.assertEqual(run(rows)["A"]["status"], "DATA_UNAVAILABLE")

    def test_only_last_500_are_used_and_input_is_not_mutated(self):
        rows = history(510, slope=2.)
        before = copy.deepcopy(rows)
        self.assertEqual(run(rows), run(rows[-500:]))
        self.assertEqual(rows, before)


class StrategyTests(unittest.TestCase):
    def test_up_down_and_wait_with_real_indicator_calculation(self):
        for slope, base, expected in ((2., 1000., "UP"), (-2., 3000., "DOWN"), (0., 1000., "WAIT")):
            with self.subTest(direction=expected):
                results = run(history(slope=slope, base=base, width=.25))
                for key in ("A", "B", "consensus"):
                    self.assertEqual(results[key]["status"], "OK")
                    self.assertEqual(results[key]["direction"], expected)
                    self.assertTrue(math.isfinite(results[key]["score"]))
                    self.assertTrue(results[key]["reason_codes"])

    def test_a_trend_thresholds_are_inclusive(self):
        for d, close, expected in ((.25, 1005., "UP"), (-.25, 995., "DOWN"),
                                   (.249, 1005., "WAIT"), (-.249, 995., "WAIT")):
            rows = history()
            rows[-1].update(open=1000., close=close,
                            high=1005. if d > 0 else 1003.,
                            low=997. if d > 0 else 995.)
            with self.subTest(d=d), fixed_emas(1000. + d * 8., 1000.):
                result = s.analyze_a(rows, now=NOW, confirmed_at=NOW)
                self.assertEqual(result["indicators"]["atr14"], 8.)
                self.assertAlmostEqual(result["score"], d)
                self.assertEqual(result["direction"], expected)

    def test_a_breakout_is_strict_and_uses_highs_lows(self):
        for close in (1004., 996.):
            rows = history()
            rows[-1] = candle(rows[-1]["time"], close)
            with self.subTest(close=close), fixed_emas(1100. if close > 1000 else 900., 1000.):
                result = s.analyze_a(rows, now=NOW, confirmed_at=NOW)
                self.assertEqual(result["direction"], "WAIT")
                self.assertIn("NO_BREAKOUT", result["reason_codes"])

    def test_a_previous_20_excludes_current_and_21st_previous(self):
        rows = history()
        rows[-22]["high"] = 2000.  # Outside the previous 20.
        rows[-1] = candle(rows[-1]["time"], 1100.)
        with fixed_emas(1100., 1000.):
            result = s.analyze_a(rows, now=NOW, confirmed_at=NOW)
            self.assertEqual(result["direction"], "UP")
            self.assertEqual(result["indicators"]["previous_high20"], 1004.)
            rows[-21]["high"] = 1500.  # Inside the previous 20.
            result = s.analyze_a(rows, now=NOW, confirmed_at=NOW)
            self.assertEqual(result["direction"], "WAIT")
            self.assertEqual(result["indicators"]["previous_high20"], 1500.)

    def test_a_requires_trend_and_breakout_to_agree(self):
        rows = history()
        rows[-1] = candle(rows[-1]["time"], 1100.)
        with fixed_emas(900., 1000.):
            self.assertEqual(s.analyze_a(rows, now=NOW, confirmed_at=NOW)["direction"], "WAIT")

    def test_atr14_uses_true_range_including_price_gaps(self):
        rows = history()
        rows[-1] = candle(rows[-1]["time"], 1100., width=2.)
        atr = run(rows)["A"]["indicators"]["atr14"]
        self.assertAlmostEqual(atr, (13 * 8. + 102.) / 14)

    def test_zero_atr_skips_a_b_remains_independent(self):
        results = run(history(width=0.))
        self.assertEqual(results["A"]["status"], "SKIPPED")
        self.assertIsNone(results["A"]["score"])
        self.assertIn("ZERO_ATR", results["A"]["reason_codes"])
        self.assertEqual(results["B"]["direction"], "WAIT")
        self.assertEqual(results["consensus"]["indicators"]["valid_count"], 1)

    def test_b_fixed_momentum_thresholds_and_four_bar_reference(self):
        for close, expected in ((1002., "UP"), (998., "DOWN"),
                                 (1001.999, "WAIT"), (998.001, "WAIT")):
            rows = history()
            rows[-1] = candle(rows[-1]["time"], close)
            with self.subTest(close=close):
                result = s.analyze_b(rows, now=NOW, confirmed_at=NOW)
                self.assertEqual(result["direction"], expected)
                self.assertAlmostEqual(result["score"], 100 * (close / rows[-5]["close"] - 1))
                self.assertEqual(set(result["indicators"]), {"ema20", "ema50", "momentum_pct"})

    def test_b_requires_ema_and_momentum_to_agree(self):
        rows = history(slope=2., width=.25)
        rows[-1] = candle(rows[-1]["time"], rows[-5]["close"] - 10.)
        result = s.analyze_b(rows, now=NOW, confirmed_at=NOW)
        self.assertGreater(result["indicators"]["ema20"], result["indicators"]["ema50"])
        self.assertLess(result["score"], -.20)
        self.assertEqual(result["direction"], "WAIT")

    def test_b_decimal_price_at_exact_threshold(self):
        for close, expected in ((30.06, "UP"), (29.94, "DOWN")):
            rows = history(base=30., width=.1)
            rows[-1] = candle(rows[-1]["time"], close, width=.1)
            with self.subTest(close=close):
                result = s.analyze_b(rows, now=NOW, confirmed_at=NOW)
                self.assertEqual(result["direction"], expected)
                self.assertEqual(abs(result["score"]), .20)

    def test_b_equal_emas_always_wait(self):
        rows = history()
        rows[-1] = candle(rows[-1]["time"], 1002.)
        with fixed_emas(1000., 1000.):
            self.assertEqual(s.analyze_b(rows, now=NOW, confirmed_at=NOW)["direction"], "WAIT")

    def test_all_results_share_hourly_base_and_exact_targets(self):
        rows = history(slope=2.)
        results = run(rows)
        for result in results.values():
            self.assertEqual(result["base_price"], rows[-1]["close"])
            self.assertEqual(result["input_close_at"], rows[-1]["close_time"] + 1)
            self.assertEqual(result["due_at"], {1: END + s.HOUR_MS, 4: END + 4 * s.HOUR_MS})
            self.assertEqual(result["thresholds_pct"], results["A"]["thresholds_pct"])
        json.dumps(results, allow_nan=False)


class ConsensusTests(unittest.TestCase):
    def test_strict_majority_and_agreement(self):
        cases = ((["UP", "UP", "DOWN"], "UP", 67),
                 (["DOWN", "DOWN", "WAIT"], "DOWN", 67),
                 (["WAIT", "WAIT", "UP"], "WAIT", 67),
                 (["UP", "DOWN"], "WAIT", 50),
                 (["UP", "WAIT"], "WAIT", 50),
                 (["UP", "DOWN", "WAIT"], "WAIT", 33),
                 (["UP", "UP", "DOWN", "WAIT"], "WAIT", 50))
        for directions, expected, agreement in cases:
            with self.subTest(directions=directions):
                result = s.consensus([vote(str(i), d) for i, d in enumerate(directions)])
                self.assertEqual(result["direction"], expected)
                self.assertEqual(result["indicators"]["agreement_pct"], agreement)
                self.assertEqual(result["score"], (directions.count("UP") - directions.count("DOWN")) / len(directions))
                self.assertNotIn("confidence", result)
                self.assertNotIn("probability", result)

    def test_technical_and_invalid_results_do_not_vote(self):
        results = [vote("A"), vote("B", None, "DATA_UNAVAILABLE", None),
                   vote("C", "DOWN", "SKIPPED", None), vote("D", "DOWN", score=float("nan")),
                   vote("E", "BUY"), vote("F", "DOWN", score=True)]
        result = s.consensus(results)
        self.assertEqual(result["direction"], "UP")
        self.assertEqual(result["indicators"]["votes"], {"UP": 1, "DOWN": 0, "WAIT": 0})
        self.assertEqual(result["indicators"]["valid_count"], 1)
        self.assertEqual(result["indicators"]["skipped_count"], 5)
        self.assertEqual(result["indicators"]["agreement_pct"], 100)

    def test_zero_valid_votes_is_unavailable_not_wait(self):
        for results in ([], [vote("A", None, "SKIPPED", None)]):
            with self.subTest(results=results):
                result = s.consensus(results)
                self.assertEqual(result["status"], "DATA_UNAVAILABLE")
                self.assertIsNone(result["direction"])
                self.assertIsNone(result["score"])
                self.assertIsNone(result["indicators"]["agreement_pct"])

    def test_duplicate_votes_and_mixed_contexts_are_rejected(self):
        a, b = vote("A"), vote("B")
        with self.assertRaises(ValueError):
            s.consensus([a, a])
        with self.assertRaises(ValueError):
            s.consensus([vote("consensus")])
        for field, value in (("base_price", 900.), ("input_close_at", END - s.HOUR_MS),
                             ("due_at", {1: 0, 4: 0}), ("thresholds_pct", {1: .2, 4: .4})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                s.consensus([a, {**b, field: value}])


class EvaluationTests(unittest.TestCase):
    def test_decimal_price_at_exact_threshold_is_flat(self):
        result = {**vote("A"), "base_price": 30., "thresholds_pct": {1: .20, 4: .20}}
        for close in (30.06, 29.94):
            with self.subTest(close=close):
                evaluated = s.evaluate_result(
                    result, 1, candle(END, close, width=.1),
                    now=END+s.HOUR_MS, confirmed_at=END+s.HOUR_MS,
                )
                self.assertEqual(evaluated["observed_direction"], "FLAT")
                self.assertIs(evaluated["correct"], False)

    def test_percentage_units_and_floor(self):
        self.assertEqual(s.evaluation_thresholds(1000., 20.), {1: .5, 4: 1.})
        self.assertEqual(s.evaluation_thresholds(1000., 0.), {1: .1, 4: .1})
        for price, atr in ((0., 1.), (100., -1.), (float("nan"), 1.), (100., float("inf"))):
            with self.subTest(price=price, atr=atr), self.assertRaises(ValueError):
                s.evaluation_thresholds(price, atr)

    def test_frozen_thresholds_strict_boundaries_flat_and_wait(self):
        for direction, actual, observed, correct in (
            ("UP", 1001., "FLAT", False), ("DOWN", 999., "FLAT", False),
            ("UP", 1001.1, "UP", True), ("DOWN", 998.9, "DOWN", True),
            ("UP", 998., "DOWN", False), ("WAIT", 1000., "FLAT", None),
            ("WAIT", 1010., "UP", None)):
            result = vote("A", direction)
            target = candle(END, actual)
            with self.subTest(direction=direction, actual=actual):
                evaluated = s.evaluate_result(result, 1, target, now=END+s.HOUR_MS, confirmed_at=END+s.HOUR_MS)
                self.assertEqual(evaluated["observed_direction"], observed)
                self.assertIs(evaluated["correct"], correct)
                self.assertEqual(result["thresholds_pct"][1], .1)

    def test_exact_hourly_targets_and_final_close_confirmation(self):
        result = vote("A")
        for horizon in (1, 4):
            due = result["due_at"][horizon]
            target = candle(due-s.HOUR_MS, 1010.)
            with self.subTest(horizon=horizon):
                early = s.evaluate_result(result, horizon, target, now=due-1, confirmed_at=due-1)
                self.assertEqual(early["reason_codes"], ["TARGET_NOT_CLOSED"])
                unconfirmed = s.evaluate_result(result, horizon, target, now=due+1000, confirmed_at=due-1)
                self.assertEqual(unconfirmed["reason_codes"], ["TARGET_CLOSE_NOT_CONFIRMED"])
                evaluated = s.evaluate_result(result, horizon, target, now=due+1000, confirmed_at=due)
                self.assertIs(evaluated["correct"], True)
                # A later hourly candle cannot replace the missed exact target.
                with self.assertRaises(ValueError):
                    s.evaluate_result(result, horizon, candle(due, 1010.), now=due+s.HOUR_MS, confirmed_at=due+s.HOUR_MS)
        minute = candle(END, 1010.)
        minute["close_time"] = END + 60_000 - 1
        with self.assertRaises(ValueError):
            s.evaluate_result(result, 1, minute, now=END+s.HOUR_MS, confirmed_at=END+s.HOUR_MS)

    def test_technical_skip_is_not_scored(self):
        result = vote("A", None, "SKIPPED", None)
        self.assertIsNone(s.evaluate_result(result, 1, {}, now=NOW, confirmed_at=NOW)["correct"])
        with self.assertRaises(ValueError):
            s.evaluate_result(vote("A"), 30, {}, now=NOW, confirmed_at=NOW)


if __name__ == "__main__":
    unittest.main()
