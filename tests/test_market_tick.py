"""One-shot job lifecycle and collection, without real Binance or Neon calls."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import service as service_module
from app.jobs import market_tick
from app.service import ForecastService, SYMBOLS
from app.storage import Store
from test_strategy_service import CLOSE, FakeMarket


@pytest.fixture
def job(monkeypatch):
    store = SimpleNamespace(init=Mock(), close=Mock())
    market = SimpleNamespace(close=AsyncMock())
    service = SimpleNamespace(cycle=AsyncMock(), errors={})
    factories = SimpleNamespace(
        store=Mock(return_value=store),
        market=Mock(return_value=market),
        service=Mock(return_value=service),
    )
    monkeypatch.setattr(market_tick, "Store", factories.store)
    monkeypatch.setattr(market_tick, "BinanceMarket", factories.market)
    monkeypatch.setattr(market_tick, "ForecastService", factories.service)
    return SimpleNamespace(
        store=store, market=market, service=service, factories=factories,
    )


def test_one_cycle_exits_successfully_and_closes_resources(job):
    assert market_tick.main() == 0
    job.factories.store.assert_called_once_with()
    job.store.init.assert_called_once_with()
    job.factories.market.assert_called_once_with()
    job.factories.service.assert_called_once_with(job.market, job.store, enable_krypta=True)
    job.service.cycle.assert_awaited_once_with()
    job.market.close.assert_awaited_once_with()
    job.store.close.assert_called_once_with()


def test_recorded_operation_errors_fail_the_job_and_are_logged(job, caplog):
    job.service.errors["BTCUSDT:quote"] = "temporary failure"
    assert market_tick.main() == 1
    assert "BTCUSDT:quote" in caplog.text
    assert "temporary failure" in caplog.text
    job.service.cycle.assert_awaited_once_with()
    job.market.close.assert_awaited_once_with()
    job.store.close.assert_called_once_with()


def test_uncaught_cycle_error_fails_without_retry_and_closes_resources(job, caplog):
    job.service.cycle.side_effect = SQLAlchemyError("database unavailable")
    assert market_tick.main() == 1
    assert any(r.message == "Market tick failed" and r.exc_info for r in caplog.records)
    job.service.cycle.assert_awaited_once_with()
    job.market.close.assert_awaited_once_with()
    job.store.close.assert_called_once_with()


def test_initialization_error_closes_store_without_starting_market(job):
    job.store.init.side_effect = SQLAlchemyError("initialization failed")
    assert market_tick.main() == 1
    job.factories.market.assert_not_called()
    job.service.cycle.assert_not_awaited()
    job.store.close.assert_called_once_with()


def test_market_construction_error_closes_store(job):
    job.factories.market.side_effect = RuntimeError("client initialization failed")
    assert market_tick.main() == 1
    job.service.cycle.assert_not_awaited()
    job.store.close.assert_called_once_with()


def test_service_construction_error_closes_both_resources(job):
    job.factories.service.side_effect = RuntimeError("service initialization failed")
    assert market_tick.main() == 1
    job.market.close.assert_awaited_once_with()
    job.store.close.assert_called_once_with()


def test_market_cleanup_error_still_closes_store(job):
    job.market.close.side_effect = RuntimeError("client cleanup failed")
    assert market_tick.main() == 1
    job.store.close.assert_called_once_with()


def test_store_cleanup_error_fails_the_job(job):
    job.store.close.side_effect = RuntimeError("database cleanup failed")
    assert market_tick.main() == 1
    job.market.close.assert_awaited_once_with()


def test_cancellation_propagates_after_cleanup(job):
    job.service.cycle.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(market_tick.run_once())
    job.market.close.assert_awaited_once_with()
    job.store.close.assert_called_once_with()


@pytest.mark.parametrize("failed_symbol", [None, "BTCUSDT", "ETHUSDT"])
def test_real_cycle_persists_both_pairs_and_restarts_without_duplicates(
    tmp_path, monkeypatch, failed_symbol,
):
    clock = {"now": CLOSE + 60_000}
    monkeypatch.setattr(service_module.time, "time", lambda: clock["now"] / 1000)
    url = "sqlite:///" + str(tmp_path / "market_tick.db")
    markets = []
    stores = []

    def make_store():
        store = Store(url)
        if failed_symbol:
            save_quote = store.save_quote

            def save(quote):
                if quote["symbol"] == failed_symbol:
                    raise SQLAlchemyError("simulated quote failure")
                return save_quote(quote)

            store.save_quote = save
        stores.append(store)
        return store

    def make_market():
        market = FakeMarket(clock)
        market.close = AsyncMock()
        markets.append(market)
        return market

    monkeypatch.setattr(market_tick, "Store", make_store)
    monkeypatch.setattr(market_tick, "BinanceMarket", make_market)
    # The production service is deliberately not mocked here.
    assert market_tick.ForecastService is ForecastService
    for _ in range(2):
        assert market_tick.main() == (1 if failed_symbol else 0)

    assert len(stores) == len(markets) == 2
    for market in markets:
        market.close.assert_awaited_once_with()
        assert [c[1] for c in market.calls if c[0] == "ticker"] == list(SYMBOLS)

    reader = Store(url)
    try:
        for symbol in SYMBOLS:
            quote = reader.get_quote(symbol)
            assert (quote is None) == (symbol == failed_symbol)
            for interval, count in (("15m", 200), ("1h", 501), ("4h", 200)):
                assert len(reader.get_candles(symbol, interval, count)) == count
            assert reader.strategy_history(symbol)["total"] == 8
            assert reader.strategy_history(symbol, strategy_id="krypta_mtf")["total"] == 2
            assert reader.history(symbol)["total"] == 3
    finally:
        reader.close()
