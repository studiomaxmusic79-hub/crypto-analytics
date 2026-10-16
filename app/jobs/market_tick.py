"""Run one existing ForecastService cycle: python -m app.jobs.market_tick.

This entry point enables Krypta MTF v1 alongside the existing service strategies.
It does not start FastAPI or schedule runs. Render supplies DATABASE_URL.
"""
import asyncio
import logging

from ..market import BinanceMarket
from ..service import ForecastService
from ..storage import Store


logger = logging.getLogger(__name__)


async def run_once():
    """Return 0 on success or 1 for a cycle with recorded operation errors."""
    store = Store()
    try:
        await asyncio.to_thread(store.init)
        market = BinanceMarket()
        try:
            service = ForecastService(market, store, enable_krypta=True)
            await service.cycle()
            if service.errors:
                for operation, error in sorted(service.errors.items()):
                    logger.error("Market tick error [%s]: %s", operation, error)
                return 1
            logger.info("Market tick completed successfully")
            return 0
        finally:
            await market.close()
    finally:
        await asyncio.to_thread(store.close)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(run_once())
    except Exception:
        logger.exception("Market tick failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
