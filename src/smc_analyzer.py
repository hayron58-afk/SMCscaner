"""SMC-анализ через библиотеку smartmoneyconcepts."""

from __future__ import annotations

from typing import Any

import pandas as pd
from smartmoneyconcepts import smc

SWING_LENGTH = 20
RECENT_OB_LOOKBACK = 30
RECENT_FVG_LOOKBACK = 30


def _prepare_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    ohlc = df[["open", "high", "low", "close", "volume"]].copy()
    ohlc.columns = ["open", "high", "low", "close", "volume"]
    return ohlc


def _last_valid_row(series: pd.Series) -> tuple[int | None, Any]:
    """Последний ненулевой/не-NaN индекс и значение."""
    valid = series.dropna()
    valid = valid[valid != 0]
    if valid.empty:
        return None, None
    idx = valid.index[-1]
    return int(idx), valid.iloc[-1]


def _get_bias(bos_choch: pd.DataFrame) -> str:
    """Определяет bias по последнему BOS."""
    bos = bos_choch["BOS"]
    idx, value = _last_valid_row(bos)
    if idx is None:
        return "neutral"
    return "bullish" if value == 1 else "bearish"


def _get_last_structure_event(bos_choch: pd.DataFrame) -> dict | None:
    """Последнее по времени событие — BOS ИЛИ CHoCH, смотря что свежее.

    Раньше здесь сначала проверялся BOS целиком и возвращался, если там
    было хоть одно ненулевое значение — даже если CHoCH случился позже и
    был более свежим событием. Теперь сравниваются индексы обоих и
    возвращается действительно последнее.
    """
    candidates: list[tuple[int, str, Any]] = []
    for col in ("BOS", "CHOCH"):
        idx, value = _last_valid_row(bos_choch[col])
        if idx is not None:
            candidates.append((idx, col, value))

    if not candidates:
        return None

    idx, col, value = max(candidates, key=lambda c: c[0])
    return {
        "type": col,
        "direction": "bullish" if value == 1 else "bearish",
        "level": float(bos_choch.loc[idx, "Level"]),
        "index": idx,
    }


def _premium_discount_zone(
    ohlc: pd.DataFrame,
    swings: pd.DataFrame,
    current_price: float,
) -> str:
    """
    Premium / discount / equilibrium по последним swing high/low.
    Классическая ICT-логика: 50% диапазона = equilibrium.
    """
    swing_highs = swings[swings["HighLow"] == 1]["Level"].dropna()
    swing_lows = swings[swings["HighLow"] == -1]["Level"].dropna()

    if swing_highs.empty or swing_lows.empty:
        return "unknown"

    recent_high = float(swing_highs.iloc[-1])
    recent_low = float(swing_lows.iloc[-1])
    if recent_high <= recent_low:
        return "unknown"

    equilibrium = (recent_high + recent_low) / 2
    if current_price < equilibrium:
        return "discount"
    if current_price > equilibrium:
        return "premium"
    return "equilibrium"


def _collect_order_blocks(ob_df: pd.DataFrame, lookback: int = RECENT_OB_LOOKBACK) -> list[dict]:
    blocks: list[dict] = []
    mask = ob_df["OB"].fillna(0) != 0
    candidates = ob_df[mask].tail(lookback) if lookback > 0 else ob_df[mask]
    for idx, row in candidates.iterrows():
        blocks.append(
            {
                "index": int(idx),
                "direction": "bullish" if row["OB"] == 1 else "bearish",
                "top": float(row["Top"]),
                "bottom": float(row["Bottom"]),
                "kind": "OB",
            }
        )
    return blocks


def _collect_fvgs(fvg_df: pd.DataFrame, lookback: int = RECENT_FVG_LOOKBACK) -> list[dict]:
    """FVG (Fair Value Gap / имбаланс) — тот же формат, что и order block'и
    (index/direction/top/bottom), плюс "kind": "FVG", чтобы дальше их можно
    было искать одной и той же функцией (см. scanner._find_zone_near_price).
    """
    zones: list[dict] = []
    mask = fvg_df["FVG"].fillna(0) != 0
    candidates = fvg_df[mask].tail(lookback) if lookback > 0 else fvg_df[mask]
    for idx, row in candidates.iterrows():
        zones.append(
            {
                "index": int(idx),
                "direction": "bullish" if row["FVG"] == 1 else "bearish",
                "top": float(row["Top"]),
                "bottom": float(row["Bottom"]),
                "kind": "FVG",
            }
        )
    return zones


def analyze_smc(df: pd.DataFrame, timeframe_label: str) -> dict:
    """
    Запускает SMC-индикаторы и возвращает структурированный результат.

    Args:
        df: OHLCV DataFrame
        timeframe_label: метка таймфрейма (H4, H1, M15...) — для логов/сообщений

    Returns:
        dict с bias, structure, order_blocks, fvgs, premium_discount, current_price
    """
    ohlc = _prepare_ohlc(df)
    current_price = float(ohlc["close"].iloc[-1])

    swings = smc.swing_highs_lows(ohlc, swing_length=SWING_LENGTH)
    bos_choch = smc.bos_choch(ohlc, swings, close_break=True)
    ob_df = smc.ob(ohlc, swings, close_mitigation=False)
    fvg_df = smc.fvg(ohlc, join_consecutive=False)
    liquidity = smc.liquidity(ohlc, swings, range_percent=0.01)

    order_blocks = _collect_order_blocks(ob_df)
    fvgs = _collect_fvgs(fvg_df)
    zone = _premium_discount_zone(ohlc, swings, current_price)

    return {
        "timeframe": timeframe_label,
        "current_price": current_price,
        "bias": _get_bias(bos_choch),
        "last_structure": _get_last_structure_event(bos_choch),
        "premium_discount": zone,
        "order_blocks": order_blocks,
        "fvgs": fvgs,
        "fvg_count": len(fvgs),
        "liquidity_df": liquidity,
        "swing_range": {
            "high": float(swings[swings["HighLow"] == 1]["Level"].dropna().iloc[-1])
            if not swings[swings["HighLow"] == 1]["Level"].dropna().empty
            else None,
            "low": float(swings[swings["HighLow"] == -1]["Level"].dropna().iloc[-1])
            if not swings[swings["HighLow"] == -1]["Level"].dropna().empty
            else None,
        },
    }


def format_ob_zone(zone: dict) -> str:
    kind = zone.get("kind", "OB")
    return f"{kind} {zone['bottom']:.4f} – {zone['top']:.4f}"
