"""Получение OHLCV-свечей с биржи через ccxt."""

from __future__ import annotations

import os
import time

import ccxt
import pandas as pd
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))


TIMEFRAME_MAP = {
    "H1": "1h",
    "H4": "4h",
    "M15": "15m",
    "D1": "1d",
}


def _to_ccxt_symbol(symbol: str) -> str:
    """BTCUSDT -> BTC/USDT."""
    if "/" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    raise ValueError(f"Не удалось распознать символ: {symbol}")


def _create_exchange() -> ccxt.Exchange:
    exchange_name = os.getenv("EXCHANGE", "binance").lower()
    exchange_class = getattr(ccxt, exchange_name, None)
    if exchange_class is None:
        raise ValueError(f"Биржа не поддерживается: {exchange_name}")

    config: dict = {"enableRateLimit": True}
    api_key = os.getenv("EXCHANGE_API_KEY")
    api_secret = os.getenv("EXCHANGE_API_SECRET")
    if api_key and api_secret:
        config["apiKey"] = api_key
        config["secret"] = api_secret

    return exchange_class(config)


_exchange: ccxt.Exchange | None = None


def _get_exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        _exchange = _create_exchange()
    return _exchange


def get_ohlcv(symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    """
    Загружает OHLCV для пары и таймфрейма.

    Args:
        symbol: например BTCUSDT или BTC/USDT
        timeframe: H1, H4, M15, D1
        limit: количество последних свечей

    Returns:
        DataFrame с колонками open, high, low, close, volume
    """
    ccxt_tf = TIMEFRAME_MAP.get(timeframe.upper())
    if ccxt_tf is None:
        raise ValueError(f"Неизвестный таймфрейм: {timeframe}")

    exchange = _get_exchange()
    ccxt_symbol = _to_ccxt_symbol(symbol)

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            raw = exchange.fetch_ohlcv(ccxt_symbol, timeframe=ccxt_tf, limit=limit)
            break
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last_error  # type: ignore[misc]
    df = pd.DataFrame(
        raw,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]
