"""Скачивает историю OHLCV с биржи через ccxt и сохраняет в CSV
в формате, который ожидает src/backtest.py:

    timestamp, open, high, low, close, volume

Использование:
    # одна монета, 180 дней H1-истории с Binance
    python -m src.fetch_history --symbols BTCUSDT --days 180

    # несколько монет сразу (как в scanner.SYMBOLS)
    python -m src.fetch_history --symbols BTCUSDT,ETHUSDT,SOLUSDT,LINKUSDT --days 365

    # другая биржа (та же, что в EXCHANGE из .env)
    python -m src.fetch_history --symbols BTCUSDT --exchange bybit --days 90

Файлы сохраняются как data/<SYMBOL>_<TIMEFRAME>.csv, например data/BTCUSDT_H1.csv —
это путь, который дальше передаётся в `--data` при запуске backtest.py.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import ccxt
import pandas as pd

# Длительность одной свечи в миллисекундах — нужна для пагинации запросов.
TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

MAX_PAGES = 2000  # защита от зацикливания при пагинации


def _to_ccxt_symbol(symbol: str) -> str:
    """BTCUSDT -> BTC/USDT. Если уже с слэшем — оставляет как есть."""
    symbol = symbol.strip().upper()
    if "/" in symbol:
        return symbol
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    raise ValueError(
        f"Не смог определить торговую пару для {symbol!r}. "
        f"Укажи явно в формате BASE/QUOTE, например BTC/USDT."
    )


def fetch_ohlcv_history(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    days: int,
    limit_per_call: int = 1000,
) -> pd.DataFrame:
    """Тянет историю свечей постранично через ccxt.fetch_ohlcv, начиная с
    (сейчас - days) и до текущего момента."""
    tf_ms = TIMEFRAME_MS.get(timeframe)
    if tf_ms is None:
        raise ValueError(
            f"Неизвестный timeframe {timeframe!r}, доступны: {list(TIMEFRAME_MS)}"
        )

    exchange_cls = getattr(ccxt, exchange_id)
    exchange = exchange_cls({"enableRateLimit": True})
    ccxt_symbol = _to_ccxt_symbol(symbol)

    since = exchange.milliseconds() - days * 24 * 60 * 60 * 1000
    all_rows: list[list] = []

    for _ in range(MAX_PAGES):
        batch = exchange.fetch_ohlcv(
            ccxt_symbol, timeframe=timeframe, since=since, limit=limit_per_call
        )
        if not batch:
            break

        all_rows.extend(batch)
        last_ts = batch[-1][0]
        next_since = last_ts + tf_ms

        # Биржа вернула тот же диапазон повторно — дальше двигаться некуда.
        if next_since <= since:
            break

        since = next_since
        if len(batch) < limit_per_call:
            # Достигли конца доступной истории (свежий край).
            break

        time.sleep(exchange.rateLimit / 1000)

    if not all_rows:
        raise RuntimeError(f"{symbol}: биржа не вернула ни одной свечи")

    df = pd.DataFrame(
        all_rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Скачать историю OHLCV через ccxt для бэктеста SMC-сканера"
    )
    parser.add_argument(
        "--symbols",
        required=True,
        help="Через запятую, например BTCUSDT,ETHUSDT,SOLUSDT",
    )
    parser.add_argument(
        "--exchange",
        default="binance",
        help="ccxt id биржи: binance, bybit, bingx и т.д. (по умолчанию binance)",
    )
    parser.add_argument(
        "--timeframe",
        default="1h",
        help="Таймфрейм свечей — для backtest.py нужен именно 1h (H1)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Сколько дней истории тянуть назад (по умолчанию 180)",
    )
    parser.add_argument(
        "--out-dir",
        default="data",
        help="Папка для сохранения CSV (по умолчанию data/)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    tf_label = args.timeframe.upper()

    for symbol in symbols:
        print(f"[{symbol}] Скачиваю {args.days} дней ({args.timeframe}) с {args.exchange}...")
        try:
            df = fetch_ohlcv_history(args.exchange, symbol, args.timeframe, args.days)
        except Exception as exc:
            print(f"[{symbol}] Ошибка: {exc}")
            continue

        out_path = out_dir / f"{symbol}_{tf_label}.csv"
        df.to_csv(out_path, index=False)
        print(f"[{symbol}] Сохранено {len(df)} свечей -> {out_path}")


if __name__ == "__main__":
    main()
