"""SMC-анализ через библиотеку smartmoneyconcepts."""

from __future__ import annotations

from typing import Any

import pandas as pd
from smartmoneyconcepts import smc

SWING_LENGTH = 20
RECENT_OB_LOOKBACK = 30


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
    """Последнее событие BOS или CHoCH."""
    for col in ("BOS", "CHOCH"):
        idx, value = _last_valid_row(bos_choch[col])
        if idx is not None:
            return {
                "type": col,
                "direction": "bullish" if value == 1 else "bearish",
                "level": float(bos_choch.loc[idx, "Level"]),
                "index": idx,
            }
    return None


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
    if lookback > 0:
        candidates = ob_df[mask].tail(lookback)
    else:
        candidates = ob_df[mask]
    for idx, row in candidates.iterrows():
        blocks.append(
            {
                "index": int(idx),
                "direction": "bullish" if row["OB"] == 1 else "bearish",
                "top": float(row["Top"]),
                "bottom": float(row["Bottom"]),
            }
        )
    return blocks


def _find_active_ob(
    order_blocks: list[dict],
    current_price: float,
    direction: str,
    tolerance_pct: float = 0.002,
) -> dict | None:
    """Ищет свежий OB нужного направления, внутри которого (или у границы) текущая цена."""
    matching = [ob for ob in order_blocks if ob["direction"] == direction]
    if not matching:
        return None

    # Берём самый свежий (последний по индексу)
    ob = max(matching, key=lambda x: x["index"])
    top, bottom = ob["top"], ob["bottom"]
    zone_height = top - bottom
    buffer = max(zone_height * tolerance_pct, top * tolerance_pct)

    if direction == "bullish":
        in_zone = (bottom - buffer) <= current_price <= (top + buffer)
    else:
        in_zone = (bottom - buffer) <= current_price <= (top + buffer)

    if in_zone:
        return ob
    return None


def _nearest_liquidity_target(
    liquidity: pd.DataFrame,
    current_price: float,
    direction: str,
) -> float | None:
    """Ближайший уровень ликвидности как цель."""
    liq = liquidity.dropna(subset=["Liquidity"])
    liq = liq[liq["Liquidity"] != 0]
    if liq.empty:
        return None

    if direction == "bullish":
        above = liq[liq["Level"] > current_price]
        if above.empty:
            return None
        return float(above["Level"].min())

    below = liq[liq["Level"] < current_price]
    if below.empty:
        return None
    return float(below["Level"].max())


def analyze_smc(df: pd.DataFrame, higher_tf: str) -> dict:
    """
    Запускает SMC-индикаторы и возвращает структурированный результат.

    Args:
        df: OHLCV DataFrame
        higher_tf: контекстный таймфрейм (H4, H1 и т.д.) — для логов/сообщений

    Returns:
        dict с bias, structure, order_blocks, premium_discount, fvg, current_price
    """
    ohlc = _prepare_ohlc(df)
    current_price = float(ohlc["close"].iloc[-1])

    swings = smc.swing_highs_lows(ohlc, swing_length=SWING_LENGTH)
    bos_choch = smc.bos_choch(ohlc, swings, close_break=True)
    ob_df = smc.ob(ohlc, swings, close_mitigation=False)
    fvg_df = smc.fvg(ohlc, join_consecutive=False)
    liquidity = smc.liquidity(ohlc, swings, range_percent=0.01)

    order_blocks = _collect_order_blocks(ob_df)
    zone = _premium_discount_zone(ohlc, swings, current_price)

    return {
        "timeframe": higher_tf,
        "current_price": current_price,
        "bias": _get_bias(bos_choch),
        "last_structure": _get_last_structure_event(bos_choch),
        "premium_discount": zone,
        "order_blocks": order_blocks,
        "active_ob": None,  # заполняется в scanner при проверке сетапа
        "fvg_count": int((fvg_df["FVG"].fillna(0) != 0).sum()),
        "liquidity_df": liquidity,
        "swing_range": {
            "high": float(swings[swings["HighLow"] == 1]["Level"].dropna().iloc[-1])
            if not swings[swings["HighLow"] == 1]["Level"].dropna().empty
            else None,
            "low": float(swings[swings["HighLow"] == -1]["Level"].dropna().iloc[-1])
            if not swings[swings["HighLow"] == -1]["Level"].dropna().empty
            else None,
        },
        "_liquidity_helper": _nearest_liquidity_target,
    }


def format_ob_zone(ob: dict) -> str:
    return f"{ob['bottom']:.4f} – {ob['top']:.4f}"
