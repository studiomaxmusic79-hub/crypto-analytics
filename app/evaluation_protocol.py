"""Frozen LIVE protocol. Changes require a new version; no strategy changes."""
from decimal import Decimal, localcontext

from .market_inputs import decimal_text

VERSION = "krypta-live-eval-v1"
PUBLICATION_VERSION = "krypta-live-eval-v1.1"
VERSIONS = (VERSION, PUBLICATION_VERSION)
TARGET_WINDOW_MS = 300_000
ENDPOINT = "/api/v3/ticker/bookTicker"


def normalize_quote(data, symbol):
    if data["symbol"] != symbol:
        raise ValueError("Unexpected bookTicker symbol")
    fields = dict(zip(("bid_price", "ask_price", "bid_qty", "ask_qty"),
                      (decimal_text(data[k]) for k in
                       ("bidPrice", "askPrice", "bidQty", "askQty"))))
    bid, ask, bq, aq = map(Decimal, fields.values())
    if bid <= 0 or ask < bid or bq <= 0 or aq <= 0:
        raise ValueError("Invalid bookTicker spread/quantity")
    with localcontext() as ctx:
        ctx.prec = max(50, len(fields["bid_price"]) + len(fields["ask_price"]) + 4)
        fields["mid_price"] = decimal_text((bid + ask) / 2)
    return fields
