"""Основной цикл SMC-сканера — архитектура v2.

Три таймфрейма с разными ролями (решили так после того, как бэктест
показал системно низкий винрейт при "естественном" RR со старой схемы
H4-контекст + H1-вход):

  - BIAS_TF (H4)      — МЯГКИЙ бонус к профилю сетапа. Раньше был
                        обязательным условием (bias+zone), из-за чего
                        сканер неделями молчал, если рынок не давал
                        точного совпадения. Теперь просто повышает
                        уверенность в сетапе, но не блокирует его.
  - STRUCTURE_TF (H1) — поиск зон реакции: order block ИЛИ fair value gap
                        (FVG/имбаланс) — что появится, то и используется.
  - TRIGGER_TF (M15)  — точка входа: свеча должна реально среагировать от
                        зоны (закрыться обратно в сторону сделки), и с
                        этого же таймфрейма считается ATR для стопа.

Stop = ATR(M15) * ATR_MULT, с защитным полом MIN_STOP_DISTANCE_PCT на
случай аномально маленького ATR (штиль на рынке).
Target = stop_distance * FIXED_RR — RR теперь ФИКСИРОВАННЫЙ по
конструкции. Раньше target брался с H4-ликвидности при стопе с H1 OB —
при узком стопе это давало "естественный" RR в десятках (мы видели
RR=217 на одном сетапе), но такой стоп выбивался рыночным шумом почти
всегда, отсюда 0-14% винрейт на бэктестах BTC/ETH/SOL/AVAX за год.
Фиксированный RR от волатильности избавляет от этого перекоса.

AMD-свип (захват ликвидности перед разворотом, см. src/indicators.py) —
тоже бонус к профилю, не обязательное условие (сознательное решение —
чтобы не отсекать валидные сделки без чистого свипа).
"""

from __future__ import annotations

import os
import sys
import time
from typing import Literal

import pandas as pd
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

os.environ.setdefault("SMC_CREDIT", "0")

from src.data_source import get_ohlcv
from src.indicators import calculate_atr, detect_liquidity_sweep
from src.rr_calc import calc_position_sizes
from src.smc_analyzer import analyze_smc, format_ob_zone
from src.telegram_bot import send_alert

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",
    "DOGEUSDT", "ADAUSDT", "TRXUSDT", "LINKUSDT", "MATICUSDT",
    "LTCUSDT", "DOTUSDT", "SHIBUSDT", "AVAXUSDT", "UNIUSDT",
    "ICPUSDT", "XLMUSDT", "INJUSDT", "ATOMUSDT", "APTUSDT",
    "OPUSDT", "ARBUSDT", "NEARUSDT", "FILUSDT", "SANDUSDT",
    "AAVEUSDT", "RUNEUSDT", "FTMUSDT", "EGLDUSDT", "GMTUSDT",
    "DYDXUSDT", "RNDRUSDT", "STXUSDT", "CRVUSDT", "GALAUSDT",
    "PEPEUSDT", "SEIUSDT", "BLURUSDT", "TIAUSDT", "PYTHUSDT",
    "JTOUSDT", "BONKUSDT", "WIFUSDT", "KASUSDT", "MANTLEUSDT",
    "IMXUSDT", "SUIUSDT", "XAIUSDT", "ENJUSDT", "ZILUSDT"
]

BIAS_TF = "H4"
STRUCTURE_TF = "H1"
TRIGGER_TF = "M15"

ATR_PERIOD = 14
ATR_MULT = 2.0  # компромисс: 1.5→2.0 — достаточный стоп без переоптимизации
FIXED_RR = 2.5
MIN_STOP_DISTANCE_PCT = 0.003  # защитный пол на случай аномально малого ATR

SWEEP_LOOKBACK = 10
SWEEP_REVERSAL_BARS = 2

VOLUME_LOOKBACK = 20
VOLUME_SPIKE_THRESHOLD = 1.5  # вернули на исходное — объём только как бонус, не фильтр
MIN_PROFILE_SCORE = 0  # все профили (AGGRESSIVE + WATCH + GRAIL) — качество в отборе вручную


def _collect_reaction_zones(structure_analysis: dict, direction: str) -> list[dict]:
    """Только FVG нужного направления — Order Blocks показали слишком низкий
    винрейт (33.3% vs 58.8% на FVG). FVG — это имбалансы, которые рынок
    должен 'заполнить', гораздо более надежный сигнал."""
    # zones = [z for z in structure_analysis["order_blocks"] if z["direction"] == direction]
    zones = [z for z in structure_analysis["fvgs"] if z["direction"] == direction]
    return zones


def _zone_contains(price: float, zone: dict, tolerance_pct: float = 0.002) -> bool:
    top, bottom = zone["top"], zone["bottom"]
    height = top - bottom
    buffer = max(height * tolerance_pct, top * tolerance_pct)
    return (bottom - buffer) <= price <= (top + buffer)


def _find_zone_near_price(zones: list[dict], price: float) -> dict | None:
    """Перебирает зоны от самой свежей к самой старой и возвращает первую,
    где реально находится цена — а не только самую недавно сформированную
    (это была реальная причина нулевых сигналов в v1, см. историю багов)."""
    ordered = sorted(zones, key=lambda z: z["index"], reverse=True)
    for zone in ordered:
        if _zone_contains(price, zone):
            return zone
    return None


def _check_reaction(last_bar: pd.Series, direction: str, zone: dict) -> bool:
    """"Реакция" = последняя M15-свеча коснулась зоны и закрылась обратно
    в сторону сделки (отбойная свеча), не закрывшись глубоко за зоной.

    Рабочее определение, принятое по умолчанию — его можно ужесточить
    (например, требовать длинную тень) или ослабить отдельно, если по
    факту статистики окажется слишком строгим или слишком мягким.
    """
    touched = last_bar["low"] <= zone["top"] and last_bar["high"] >= zone["bottom"]
    if not touched:
        return False
    if direction == "bullish":
        return last_bar["close"] > last_bar["open"] and last_bar["close"] >= zone["bottom"]
    return last_bar["close"] < last_bar["open"] and last_bar["close"] <= zone["top"]


def _calc_volume_metrics(df_trigger: pd.DataFrame, lookback: int = VOLUME_LOOKBACK) -> dict:
    if "volume" not in df_trigger.columns:
        raise ValueError("Trigger DataFrame must contain 'volume' column")

    recent = df_trigger["volume"].tail(lookback)
    avg_volume = float(recent.mean())
    last_volume = float(df_trigger["volume"].iloc[-1])
    rel_volume = last_volume / avg_volume if avg_volume > 0 else None

    return {"avg_volume": avg_volume, "last_volume": last_volume, "rel_volume": rel_volume}


def _bias_matches(bias_analysis: dict, direction: str) -> bool:
    expected_zone = "discount" if direction == "bullish" else "premium"
    return bias_analysis["bias"] == direction and bias_analysis["premium_discount"] == expected_zone


def _profile_from_score(score: int) -> str:
    if score >= 3:
        return "GRAIL"
    if score >= 1:
        return "WATCH"
    return "AGGRESSIVE"


def _build_reason(zone: dict, bias_ok: bool, amd_ok: bool, volume_ok: bool) -> str:
    parts = [f"{STRUCTURE_TF} {zone['kind']} + {TRIGGER_TF} реакция"]
    if bias_ok:
        parts.append(f"{BIAS_TF} bias совпадает")
    if amd_ok:
        parts.append("AMD-свип ликвидности")
    if volume_ok:
        parts.append("повышенный объём")
    return " + ".join(parts)


def check_setup(
    symbol: str,
    direction: Literal["bullish", "bearish"],
    bias_analysis: dict,
    structure_analysis: dict,
    df_trigger: pd.DataFrame,
) -> dict | None:
    zones = _collect_reaction_zones(structure_analysis, direction)
    if not zones:
        return None

    price = float(df_trigger["close"].iloc[-1])
    zone = _find_zone_near_price(zones, price)
    if zone is None:
        return None

    last_bar = df_trigger.iloc[-1]
    if not _check_reaction(last_bar, direction, zone):
        return None

    atr_series = calculate_atr(df_trigger, period=ATR_PERIOD)
    current_atr = atr_series.iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        return None

    stop_distance = max(float(current_atr) * ATR_MULT, price * MIN_STOP_DISTANCE_PCT)
    entry = price

    if direction == "bullish":
        stop = entry - stop_distance
        target = entry + stop_distance * FIXED_RR
    else:
        stop = entry + stop_distance
        target = entry - stop_distance * FIXED_RR

    bias_ok = _bias_matches(bias_analysis, direction)
    amd_ok = detect_liquidity_sweep(df_trigger, direction, SWEEP_LOOKBACK, SWEEP_REVERSAL_BARS)
    volume_metrics = _calc_volume_metrics(df_trigger)
    volume_ok = (volume_metrics["rel_volume"] or 0) >= VOLUME_SPIKE_THRESHOLD

    score = int(bias_ok) + int(amd_ok) + int(volume_ok)
    
    # Фильтруем по минимальному профилю — только WATCH/GRAIL (score >= 1)
    # AGGRESSIVE (score < 1) показал 52.7% винрейта, слишком низко
    if score < MIN_PROFILE_SCORE:
        return None
    
    direction_label: Literal["LONG", "SHORT"] = "LONG" if direction == "bullish" else "SHORT"

    setup = {
        "symbol": symbol,
        "direction": direction_label,
        "timeframe_context": BIAS_TF,
        "timeframe_structure": STRUCTURE_TF,
        "timeframe_entry": TRIGGER_TF,
        "bias": bias_analysis["bias"],
        "bias_aligned": bias_ok,
        "zone": bias_analysis["premium_discount"],
        "order_block_zone": format_ob_zone(zone),
        "zone_kind": zone["kind"],
        "amd_sweep": amd_ok,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr_value": FIXED_RR,
        "profile": _profile_from_score(score),
        "reason": _build_reason(zone, bias_ok, amd_ok, volume_ok),
    }
    setup.update(volume_metrics)
    setup["position_sizes"] = calc_position_sizes(entry=entry, stop=stop, risk_pct=0.0025)
    return setup


def find_setups(
    symbol: str,
    df_bias: pd.DataFrame,
    df_structure: pd.DataFrame,
    df_trigger: pd.DataFrame,
    *,
    return_analysis: bool = False,
) -> list[dict] | tuple[list[dict], dict, dict]:
    """
    Ищет LONG/SHORT сетапы по трём таймфреймам (bias / structure / trigger).

    Используется боевым сканером и бэктестом — одна и та же логика проверок.
    """
    bias_analysis = analyze_smc(df_bias, BIAS_TF)
    structure_analysis = analyze_smc(df_structure, STRUCTURE_TF)

    setups: list[dict] = []
    for direction in ("bullish", "bearish"):
        setup = check_setup(symbol, direction, bias_analysis, structure_analysis, df_trigger)
        if setup:
            setups.append(setup)

    if return_analysis:
        return setups, bias_analysis, structure_analysis
    return setups


def _no_setup_reason(
    bias_analysis: dict,
    structure_analysis: dict,
    df_trigger: pd.DataFrame,
) -> str:
    """Краткая причина отсутствия сетапа по каждому направлению."""
    price = float(df_trigger["close"].iloc[-1])
    last_bar = df_trigger.iloc[-1]
    current_atr = calculate_atr(df_trigger, period=ATR_PERIOD).iloc[-1]

    reasons = []
    for direction in ("bullish", "bearish"):
        zones = _collect_reaction_zones(structure_analysis, direction)
        if not zones:
            reasons.append(f"{direction}: нет OB/FVG на {STRUCTURE_TF}")
            continue
        zone = _find_zone_near_price(zones, price)
        if zone is None:
            reasons.append(f"{direction}: цена вне доступных зон")
            continue
        if not _check_reaction(last_bar, direction, zone):
            reasons.append(f"{direction}: цена в зоне {zone['kind']}, но нет реакции свечой")
            continue
        if pd.isna(current_atr) or current_atr <= 0:
            reasons.append(f"{direction}: ATR недоступен (мало данных на {TRIGGER_TF})")
            continue
        reasons.append(f"{direction}: все условия сошлись — проверь логику check_setup")
    return "; ".join(reasons)


def _log_symbol(
    symbol: str,
    bias_analysis: dict,
    structure_analysis: dict,
    df_trigger: pd.DataFrame,
    setups: list[dict],
) -> None:
    print(
        f"[{symbol}] {BIAS_TF} bias: {bias_analysis['bias']} (бонус) | "
        f"{STRUCTURE_TF} зон: OB={len(structure_analysis['order_blocks'])} "
        f"FVG={len(structure_analysis['fvgs'])}"
    )

    if not setups:
        print(f"[{symbol}] {TRIGGER_TF}: {_no_setup_reason(bias_analysis, structure_analysis, df_trigger)}")
        return

    for setup in setups:
        print(f"[{symbol}] {setup['direction']} сетап найден [{setup['profile']}]")
        print(f"{setup['zone_kind']} на {STRUCTURE_TF}: {setup['order_block_zone']}")
        print(
            f"Entry: {setup['entry']:.4f}, Stop: {setup['stop']:.4f}, "
            f"Target: {setup['target']:.4f}, RR = {setup['rr_value']:.2f}"
        )
        print(f"Причина: {setup['reason']}")


def scan_once(symbols: list[str] | None = None) -> list[dict]:
    """Один проход по списку монет. Возвращает найденные сетапы."""
    symbols = symbols or SYMBOLS
    setups: list[dict] = []

    for symbol in symbols:
        try:
            df_bias = get_ohlcv(symbol, BIAS_TF)
            df_structure = get_ohlcv(symbol, STRUCTURE_TF)
            df_trigger = get_ohlcv(symbol, TRIGGER_TF)

            symbol_setups, bias_analysis, structure_analysis = find_setups(
                symbol, df_bias, df_structure, df_trigger, return_analysis=True
            )
            setups.extend(symbol_setups)

            _log_symbol(symbol, bias_analysis, structure_analysis, df_trigger, symbol_setups)
            print()

        except Exception as exc:
            print(f"[{symbol}] Ошибка: {exc}")
            print()

    return setups


def run_scanner(loop: bool = True) -> None:
    """Запускает сканер в цикле или один раз."""
    interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
    dry_run = DRY_RUN

    print("SMC Market Scanner v2 запущен")
    print(f"Биржа: {os.getenv('EXCHANGE', 'binance')}")
    print(f"Таймфреймы: bias={BIAS_TF}, structure={STRUCTURE_TF}, trigger={TRIGGER_TF}")
    print(f"Интервал: {interval}s | DRY_RUN: {dry_run}")
    print(f"Монеты: {', '.join(SYMBOLS)}")
    print()

    while True:
        setups = scan_once()

        if setups and not dry_run:
            for setup in setups:
                send_alert(setup)

        if not loop:
            break

        print(f"Следующий проход через {interval} сек...")
        time.sleep(interval)


if __name__ == "__main__":
    once = "--once" in sys.argv
    run_scanner(loop=not once)
