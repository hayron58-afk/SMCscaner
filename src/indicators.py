"""Технические индикаторы общего назначения — не специфичные для SMC-библиотеки.

Отдельно от smc_analyzer.py, потому что не зависят от smartmoneyconcepts:
- calculate_atr — для расчёта стопа, привязанного к волатильности, а не к
  сырой (и потенциально вырожденно узкой) границе order block.
- detect_liquidity_sweep — упрощённый детектор AMD-свипа (захвата
  ликвидности перед разворотом). Используется как ДОПОЛНИТЕЛЬНЫЙ бонус к
  профилю сетапа, не как обязательное условие входа.
"""

from __future__ import annotations

import pandas as pd


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR (Average True Range) со сглаживанием Уайлдера.

    df должен содержать колонки high, low, close.
    Первые `period` значений результата — NaN (недостаточно данных для
    прогрева сглаживания).
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Сглаживание Уайлдера эквивалентно EMA с alpha = 1/period.
    return true_range.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """RSI (Relative Strength Index) со сглаживанием Уайлдера.

    df должен содержать колонку close.
    """
    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return rsi


def calculate_macd(
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD: возвращает (macd_line, signal_line, histogram).

    df должен содержать колонку close.
    """
    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def detect_liquidity_sweep(
    df: pd.DataFrame,
    direction: str,
    lookback: int = 10,
    reversal_bars: int = 2,
) -> bool:
    """Простой детектор AMD-свипа (захвата ликвидности перед разворотом).

    Идея: за последние `lookback` баров (не считая самых свежих
    `reversal_bars`) цена ставит новый экстремум — минимум для bullish-сетапа
    (сняли стопы продавцов / лимитки на покупку ниже), максимум для
    bearish (сняли стопы покупателей) — а затем в течение `reversal_bars`
    баров цена закрывается обратно за этим экстремумом.

    direction: "bullish" или "bearish" — направление предполагаемой сделки.

    Возвращает bool. Используется как бонус к профилю сетапа (см.
    _apply_amd_bonus в scanner.py), НЕ как обязательное условие входа —
    так решили сознательно, чтобы не отсекать валидные сделки без чистого
    свипа.
    """
    total_needed = lookback + reversal_bars + 1
    if len(df) < total_needed:
        return False

    window = df.iloc[-total_needed : -(reversal_bars + 1)]
    recent = df.iloc[-(reversal_bars + 1) :]

    if window.empty or recent.empty:
        return False

    if direction == "bullish":
        prior_low = window["low"].min()
        swept = recent["low"].min() < prior_low
        reclaimed = recent["close"].iloc[-1] > prior_low
        return bool(swept and reclaimed)

    prior_high = window["high"].max()
    swept = recent["high"].max() > prior_high
    reclaimed = recent["close"].iloc[-1] < prior_high
    return bool(swept and reclaimed)
