"""Integration with real temporary SQLite and deterministic fake Binance data."""
import asyncio

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import SQLAlchemyError

from app import service as service_module
from app.market import INTERVALS, MarketError
from app.service import ForecastService, SYMBOLS
from app.storage import Store, market_candles, market_quotes
from test_strategy_storage import record


HOUR = 3_600_000
CLOSE = 600 * HOUR


class FakeMarket:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.fail = set()
        self.missing = set()
        self.wrong_target = False
        self.flat = False

    def bar(self, symbol, interval, start):
        price = 1000. if self.flat else 1000. + start / HOUR + (100 if symbol == 'ETHUSDT' else 0)
        if interval == '1m':
            price = 7777.  # Must never become the strategy base or target price.
        width = 0. if self.flat else .1
        return [start, price, price+width, price-width, price, 10., start+INTERVALS[interval]-1]

    async def time_ms(self):
        return self.clock['now']

    async def ticker(self, symbol):
        self.calls.append(('ticker', symbol))
        return dict(symbol=symbol, price=99999., change_pct=1., high=100000., low=90000.,
                    volume=100., updated_at=self.clock['now'])

    async def klines(self, symbol, interval, limit=200, **params):
        self.calls.append(('klines', symbol, interval, limit, params))
        if (symbol, interval) in self.fail:
            raise MarketError('temporary Binance failure')
        if 'startTime' in params:
            due = params['endTime']+1
            if (symbol, due) in self.missing:
                return []
            start = params['startTime'] + (HOUR if self.wrong_target else 0)
            return [self.bar(symbol, interval, start)]
        delta = INTERVALS[interval]
        end = params.get('endTime', self.clock['now']) // delta * delta
        return [self.bar(symbol, interval, t)
                for t in range(end-(limit-1)*delta, end+1, delta)]

    async def target_price(self, symbol, due_at, now):
        self.calls.append(('legacy_target', symbol, due_at))
        return 7777.


@pytest.fixture
def env(tmp_path, monkeypatch):
    clock = {'now': CLOSE+10_000}
    monkeypatch.setattr(service_module.time, 'time', lambda: clock['now']/1000)
    store = Store('sqlite:///' + str(tmp_path/'integration.db'))
    store.init()
    market = FakeMarket(clock)
    service = ForecastService(market, store)
    try:
        yield clock, store, market, service
    finally:
        store.close()


def run(coroutine):
    return asyncio.run(coroutine)


def rows(store, symbol='BTCUSDT', close=CLOSE):
    return [r for r in store.strategy_history(symbol)['items'] if r['input_close_at'] == close]


def exact_calls(market):
    return [c for c in market.calls if c[0] == 'klines' and 'startTime' in c[4]]


def test_cycle_collects_501_and_publishes_both_pairs_without_affecting_legacy(env):
    clock, store, market, service = env
    service.set_active_symbol('SOLUSDT')
    run(service.cycle())
    for symbol in SYMBOLS:
        saved = store.get_candles(symbol, '1h', 501)
        assert len(saved) == 501
        assert sum(c['close_time'] < clock['now'] for c in saved) == 500
        issued = rows(store, symbol)
        assert len(issued) == 6
        assert {r['strategy_id'] for r in issued} == {'trend_breakout', 'trend_momentum', 'consensus'}
        assert {r['horizon'] for r in issued} == {1, 4}
        assert {r['issued_at'] for r in issued} == {clock['now']}
        assert {r['base_price'] for r in issued} == {market.bar(symbol, '1h', CLOSE-HOUR)[4]}
        assert all(r['due_at'] == CLOSE+r['horizon']*HOUR for r in issued)
        assert len(store.latest(symbol)) == 3
        assert {r['model_version'] for r in store.latest(symbol)} == {'technical-v1'}
    assert len([c for c in market.calls if c[0] == 'klines' and c[2:4] == ('1h', 501)]) == 2
    assert len([c for c in market.calls if c[0] == 'klines' and c[2] == '1m']) == 2  # legacy only
    assert service.active_symbol == 'SOLUSDT'


@pytest.mark.parametrize('failed_symbol', SYMBOLS)
def test_quote_database_error_rolls_back_and_other_pair_still_updates(env, caplog, failed_symbol):
    clock, store, market, service = env
    healthy_symbol = next(symbol for symbol in SYMBOLS if symbol != failed_symbol)
    original = dict(symbol=failed_symbol, price=77., change_pct=0., high=78., low=76.,
                    volume=10., updated_at=clock['now']-1000, fetched_at=clock['now']-1000)
    store.save_quote(original)
    failed_connection = []
    rolled_back = []

    def fail_after_quote_write(conn, cursor, statement, parameters, context, executemany):
        if (context.isinsert and context.compiled.statement.table is market_quotes
                and context.compiled_parameters[0]['symbol'] == failed_symbol):
            failed_connection.append(conn)
            # The real upsert has run; engine.begin() must roll it back.
            raise SQLAlchemyError('simulated quote transaction failure')

    def record_rollback(conn):
        if failed_connection and conn is failed_connection[-1]:
            rolled_back.append(conn)

    event.listen(store.engine, 'after_cursor_execute', fail_after_quote_write)
    event.listen(store.engine, 'rollback', record_rollback)
    try:
        with caplog.at_level('ERROR', logger='app.service'):
            run(service.cycle())
    finally:
        event.remove(store.engine, 'after_cursor_execute', fail_after_quote_write)
        event.remove(store.engine, 'rollback', record_rollback)

    key = f'{failed_symbol}:quote'
    assert failed_connection and rolled_back
    assert store.get_quote(failed_symbol) == original
    assert store.get_quote(healthy_symbol)['price'] == 99999.
    assert [call[1] for call in market.calls if call[0] == 'ticker'] == list(SYMBOLS)
    for interval, count in (('15m', 200), ('1h', 501), ('4h', 200)):
        assert len(store.get_candles(healthy_symbol, interval, count)) == count
    assert len(rows(store, healthy_symbol)) == 6
    assert len(store.latest(healthy_symbol)) == 3
    assert service.last_cycle_at == clock['now']
    assert 'SQLAlchemyError' in service.errors[key]
    assert 'simulated quote transaction failure' in service.errors[key]
    assert any(key in record.getMessage() and record.exc_info for record in caplog.records)

    # A successful next attempt clears the error through the existing path.
    clock['now'] += 120_000
    run(service.cycle())
    assert store.get_quote(failed_symbol)['price'] == 99999.
    assert key not in service.errors


def test_repeated_cycles_and_restart_do_not_duplicate_or_recalculate_release(env, monkeypatch):
    clock, store, market, service = env
    run(service.cycle())
    before = store.strategy_history('BTCUSDT')
    def forbidden(*args, **kwargs):
        raise AssertionError('published release must not be recalculated')
    monkeypatch.setattr(service_module, 'analyze_strategies', forbidden)
    clock['now'] += 120_000
    run(service.cycle())
    restarted = ForecastService(market, store)
    run(restarted.cycle())
    assert store.strategy_history('BTCUSDT') == before
    assert len(rows(store, 'ETHUSDT')) == 6


@pytest.mark.parametrize('delay,expected', [(0, 6), (300_000, 6), (300_001, 0), (30*60_000, 0)])
def test_live_window_including_boundary_and_sleep(env, delay, expected):
    clock, store, market, service = env
    clock['now'] = CLOSE+delay
    run(service.cycle())
    assert len(rows(store)) == expected
    if not expected:
        clock['now'] = CLOSE+HOUR+10_000
        run(service.cycle())
        assert rows(store) == []
        assert len(rows(store, close=CLOSE+HOUR)) == 6


def test_work_finishing_after_live_deadline_is_not_published(env, monkeypatch):
    clock, store, market, service = env
    clock['now'] = CLOSE+299_999
    original = service_module.analyze_strategies
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        clock['now'] += 2
        return result
    monkeypatch.setattr(service_module, 'analyze_strategies', slow)
    run(service.cycle())
    assert rows(store) == []
    assert rows(store, 'ETHUSDT') == []


def test_200_bar_database_bootstraps_501_then_fetches_only_needed_tail(env):
    clock, store, market, service = env
    old = run(market.klines('BTCUSDT', '1h', 200, endTime=clock['now']))
    store.save_candles('BTCUSDT', '1h', service.candle_values(old))
    market.calls.clear()
    run(service.collect_candles('BTCUSDT', '1h', clock['now']))
    assert market.calls[0][3] == 501
    clock['now'] += 16*60_000
    market.calls.clear()
    run(service.collect_candles('BTCUSDT', '1h', clock['now']))
    assert market.calls[0][3] == 2
    clock['now'] += 3*HOUR
    market.calls.clear()
    run(service.collect_candles('BTCUSDT', '1h', clock['now']))
    assert market.calls[0][3] == 4
    saved = store.get_candles('BTCUSDT', '1h', 501)
    assert all(b['time']-a['time'] == HOUR for a,b in zip(saved,saved[1:]))


def test_internal_gap_restores_full_hourly_history(env):
    clock, store, market, service = env
    run(service.collect_candles('BTCUSDT', '1h', clock['now']))
    gap = store.get_candles('BTCUSDT', '1h', 501)[-40]['time']
    with store.engine.begin() as conn:
        conn.execute(delete(market_candles).where(
            market_candles.c.symbol == 'BTCUSDT', market_candles.c.interval == '1h',
            market_candles.c.time == gap))
    clock['now'] += 16*60_000
    market.calls.clear()
    run(service.collect_candles('BTCUSDT', '1h', clock['now']))
    assert market.calls[0][3] == 501
    assert len(store.get_candles('BTCUSDT', '1h', 501)) == 501


def test_failed_hourly_refresh_blocks_strategies_then_recovers_within_live_window(env):
    clock, store, market, service = env
    clock['now'] = CLOSE-5*60_000
    run(service.cycle())
    market.fail.add(('BTCUSDT', '1h'))
    clock['now'] = CLOSE+60_000
    run(service.cycle())
    assert rows(store) == []
    assert len(rows(store, 'ETHUSDT')) == 6
    market.fail.clear()
    clock['now'] += 120_000
    run(service.cycle())
    assert len(rows(store)) == 6


def test_missing_confirmation_never_publishes_saved_forming_snapshot(env):
    clock, store, market, service = env
    old_now = CLOSE-60_000
    raw = run(market.klines('BTCUSDT', '1h', 501, endTime=old_now))
    store.save_candles('BTCUSDT', '1h', service.candle_values(raw))
    service.candle_updates[('BTCUSDT', '1h')] = old_now
    with pytest.raises(MarketError, match='подтверждены'):
        run(service.generate_strategies('BTCUSDT', clock['now']))
    assert rows(store) == []


def test_atomic_write_failure_retries_without_occupying_live_key(env, monkeypatch):
    clock, store, market, service = env
    original = store.save_strategy_forecasts
    def fail_once(records):
        monkeypatch.setattr(store, 'save_strategy_forecasts', original)
        raise ValueError('temporary transaction failure')
    monkeypatch.setattr(store, 'save_strategy_forecasts', fail_once)
    run(service.cycle())
    assert rows(store) == []
    clock['now'] += 120_000
    run(service.cycle())
    assert len(rows(store)) == 6


def test_partial_existing_release_is_not_completed_with_different_issue_time(env):
    clock, store, market, service = env
    store.save_strategy_forecasts([record(input_close_at=CLOSE, issued_at=CLOSE,
                                         due_at=CLOSE+HOUR)])
    run(service.cycle())
    assert len(rows(store)) == 1
    assert 'BTCUSDT:strategies' in service.errors


def test_exact_one_and_four_hour_closes_are_settled_from_shared_candles(env):
    clock, store, market, service = env
    run(service.cycle())
    frozen = {r['id']: r['neutral_pct'] for r in rows(store)}
    for horizon in (1,4):
        clock['now'] = CLOSE+horizon*HOUR+10_000
        market.calls.clear()
        run(service.cycle())
        for symbol in SYMBOLS:
            checked = [r for r in rows(store,symbol) if r['horizon'] == horizon]
            assert all(r['evaluated_at'] is not None for r in checked)
            expected = market.bar(symbol,'1h',CLOSE+(horizon-1)*HOUR)[4]
            assert {r['actual_price'] for r in checked} == {expected}
            if horizon == 1:
                assert all(r['evaluated_at'] is None for r in rows(store,symbol) if r['horizon']==4)
        assert exact_calls(market) == []
    assert all(r['neutral_pct'] == frozen[r['id']] for r in rows(store))


def test_restart_resumes_old_checks_without_backfilled_live_releases(env):
    clock, store, market, service = env
    run(service.cycle())
    url = store.engine.url
    store.close()
    reopened = Store(url)
    try:
        reopened.init()
        restarted = ForecastService(market,reopened)
        clock['now'] = CLOSE+10*HOUR+10*60_000
        market.calls.clear()
        run(restarted.cycle())
        for symbol in SYMBOLS:
            assert reopened.strategy_history(symbol)['total'] == 6
            assert all(r['evaluated_at'] is not None for r in rows(reopened,symbol))
    finally:
        reopened.close()


def test_restart_without_window_confirmation_queries_each_exact_target_only_once(env):
    clock, store, market, service = env
    run(service.cycle())
    restarted = ForecastService(market,store)
    clock['now'] = CLOSE+4*HOUR+10_000
    market.calls.clear()
    run(restarted.settle_strategies(clock['now']))
    assert len(exact_calls(market)) == 4  # two symbols, two targets, twelve forecasts
    assert restarted.candle_updates == {}
    assert all(r['evaluated_at'] is not None for symbol in SYMBOLS for r in rows(store,symbol))
    market.calls.clear()
    run(restarted.settle_strategies(clock['now']))
    assert exact_calls(market) == []


def test_missing_or_wrong_target_stays_pending_then_recovers(env):
    clock, store, market, service = env
    run(service.cycle())
    restarted = ForecastService(market,store)
    clock['now'] = CLOSE+HOUR+10_000
    market.missing.add(('BTCUSDT',CLOSE+HOUR))
    run(restarted.settle_strategies(clock['now']))
    btc = [r for r in rows(store) if r['horizon']==1]
    assert all(r['evaluated_at'] is None and r['evaluation_note'] for r in btc)
    assert all(r['evaluated_at'] is not None for r in rows(store,'ETHUSDT') if r['horizon']==1)
    market.missing.clear()
    market.wrong_target = True
    run(restarted.settle_strategies(clock['now']))
    assert all(r['evaluated_at'] is None for r in rows(store) if r['horizon']==1)
    market.wrong_target = False
    run(restarted.settle_strategies(clock['now']))
    assert all(r['evaluated_at'] is not None and r['evaluation_note'] is None for r in rows(store) if r['horizon']==1)


def test_wait_and_skipped_are_never_wins_in_live_pipeline(env):
    clock, store, market, service = env
    market.flat = True
    run(service.cycle())
    clock['now'] = CLOSE+4*HOUR+10*60_000
    run(service.cycle())
    for symbol in SYMBOLS:
        saved = rows(store,symbol)
        assert all(r['correct'] is None for r in saved)
        skipped = [r for r in saved if r['strategy_id']=='trend_breakout']
        assert all(r['status']=='SKIPPED' and r['evaluated_at'] is None for r in skipped)
        waits = [r for r in saved if r['direction']=='WAIT']
        assert all(r['evaluated_at'] is not None and r['observed_direction']=='FLAT' for r in waits)


def test_unknown_evaluation_version_stays_pending(env):
    clock, store, market, service = env
    store.save_strategy_forecasts([record(input_close_at=CLOSE, issued_at=CLOSE,
                                         due_at=CLOSE+HOUR, evaluation_version='future-v2')])
    clock['now'] = CLOSE+HOUR+10_000
    run(service.settle_strategies(clock['now']))
    saved = rows(store)[0]
    assert saved['evaluated_at'] is None
    assert saved['evaluation_note']
    assert exact_calls(market) == []
