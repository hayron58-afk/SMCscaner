"""Основной цикл SMC-сканера."""

from __future__ import annotations

import os
import sys
import time
from typing import Literal

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
from src.rr_calc import calculate_rr, calc_position_sizes
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


CONTEXT_TF = "H4"
ENTRY_TF = "H1"
MIN_RR = 1.5


def _price_in_ob(current_price: float, ob: dict, tolerance_pct: float = 0.002) -> bool:
    top, bottom = ob["top"], ob["bottom"]
    zone_height = top - bottom
    buffer = max(zone_height * tolerance_pct, top * tolerance_pct)
    return (bottom - buffer) <= current_price <= (top + buffer)


def _find_fresh_ob(order_blocks: list[dict], direction: str) -> dict | None:
    matching = [ob for ob in order_blocks if ob["direction"] == direction]
    if not matching:
        return None
    return max(matching, key=lambda x: x["index"])


def _compute_target(
    h4_analysis: dict,
    side: Literal["long", "short"],
    entry: float,
) -> float:
    """Цель: ближайшая H4-ликвидность или swing high/low."""
    liquidity_df = h4_analysis.get("liquidity_df")
    helper = h4_analysis.get("_liquidity_helper")

    if liquidity_df is not None and helper is not None:
        direction = "bullish" if side == "long" else "bearish"
        liq_target = helper(liquidity_df, entry, direction)
        if liq_target is not None:
            return liq_target

    swing = h4_analysis.get("swing_range") or {}
    if side == "long" and swing.get("high"):
        return float(swing["high"])
    if side == "short" and swing.get("low"):
        return float(swing["low"])

    if side == "long":
        return entry * 1.03
    return entry * 0.97


def _profile_from_rr(rr: float) -> str:
    if rr >= 3.0:
        return "GRAIL"
    if rr >= 2.0:
        return "WATCH"
    return "AGGRESSIVE"


def _reason_from_direction(direction: Literal["LONG", "SHORT"]) -> str:
    if direction == "LONG":
        return "H4 discount + H1 OB + RR>2"
    return "H4 premium + H1 OB + RR>2"


def _calc_h1_volume(df_h1) -> dict:
    """Средний и относительный объём последних H1 свечей."""
    if "volume" not in df_h1.columns:
        raise ValueError("H1 DataFrame must contain 'volume' column")

    recent = df_h1["volume"].tail(20)
    avg_volume_h1 = float(recent.mean())
    last_volume_h1 = float(df_h1["volume"].iloc[-1])
    rel_volume_h1 = last_volume_h1 / avg_volume_h1 if avg_volume_h1 > 0 else None

    return {
        "avg_volume_h1": avg_volume_h1,
        "last_volume_h1": last_volume_h1,
        "rel_volume_h1": rel_volume_h1,
    }


def _apply_volume_impact(setup: dict) -> None:
    rel = setup.get("rel_volume_h1")
    if rel is None:
        return
    if rel >= 1.5:
        setup["reason"] += " + повышенный объём"
    elif rel <= 0.5:
        if setup["profile"] == "WATCH":
            setup["profile"] = "AGGRESSIVE"
        setup["reason"] += " (объём ниже среднего)"


def _build_setup(
    symbol: str,
    direction: Literal["LONG", "SHORT"],
    h4: dict,
    h1: dict,
    ob: dict,
    entry: float,
    stop: float,
    target: float,
    rr: float,
) -> dict:
    setup = {
        "symbol": symbol,
        "direction": direction,
        "timeframe_context": CONTEXT_TF,
        "timeframe_entry": ENTRY_TF,
        "bias": h4["bias"],
        "zone": h4["premium_discount"],
        "order_block_zone": format_ob_zone(ob),
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr_value": rr,
        "profile": _profile_from_rr(rr),
        "reason": _reason_from_direction(direction),
    }
    setup["position_sizes"] = calc_position_sizes(
        entry=setup["entry"],
        stop=setup["stop"],
        risk_pct=0.0025,
    )
    return setup


def check_long_setup(symbol: str, h4: dict, h1: dict) -> dict | None:
    if h4["bias"] != "bullish" or h4["premium_discount"] != "discount":
        return None

    ob = _find_fresh_ob(h1["order_blocks"], "bullish")
    if ob is None:
        return None

    price = h1["current_price"]
    if not _price_in_ob(price, ob):
        return None

    entry = price
    stop = ob["bottom"]
    target = _compute_target(h4, "long", entry)
    rr = calculate_rr(entry, stop, target)

    if rr < MIN_RR:
        return None

    return _build_setup(symbol, "LONG", h4, h1, ob, entry, stop, target, rr)


def check_short_setup(symbol: str, h4: dict, h1: dict) -> dict | None:
    if h4["bias"] != "bearish" or h4["premium_discount"] != "premium":
        return None

    ob = _find_fresh_ob(h1["order_blocks"], "bearish")
    if ob is None:
        return None

    price = h1["current_price"]
    if not _price_in_ob(price, ob):
        return None

    entry = price
    stop = ob["top"]
    target = _compute_target(h4, "short", entry)
    rr = calculate_rr(entry, stop, target)

    if rr < MIN_RR:
        return None

    return _build_setup(symbol, "SHORT", h4, h1, ob, entry, stop, target, rr)


def _h1_no_setup_reason(h4: dict, h1: dict) -> str:
    """Краткая причина отсутствия сетапа на H1."""
    price = h1["current_price"]

    long_ok_h4 = h4["bias"] == "bullish" and h4["premium_discount"] == "discount"
    short_ok_h4 = h4["bias"] == "bearish" and h4["premium_discount"] == "premium"

    if not long_ok_h4 and not short_ok_h4:
        return "контекст H4 не подходит — сетап отсутствует"

    for side, direction, ok_h4 in (
        ("long", "bullish", long_ok_h4),
        ("short", "bearish", short_ok_h4),
    ):
        if not ok_h4:
            continue
        ob = _find_fresh_ob(h1["order_blocks"], direction)
        if ob is None:
            return "нет подходящего order block — сетап отсутствует"
        if not _price_in_ob(price, ob):
            return f"цена вне order block ({format_ob_zone(ob)}) — сетап отсутствует"
        entry = price
        stop = ob["bottom"] if side == "long" else ob["top"]
        target = _compute_target(h4, side, entry)
        rr = calculate_rr(entry, stop, target)
        if rr < MIN_RR:
            return f"RR={rr:.2f} ниже MIN_RR={MIN_RR} — сетап отсутствует"

    return "нет подходящего order block — сетап отсутствует"


def _log_symbol(symbol: str, h4: dict, h1: dict, setups: list[dict]) -> None:
    print(f"[{symbol}] H4 bias: {h4['bias']}, zone: {h4['premium_discount']}")

    if not setups:
        reason = _h1_no_setup_reason(h4, h1)
        print(f"[{symbol}] H1: {reason}")
        return

    for setup in setups:
        print(f"[{symbol}] {setup['direction']} сетап найден [{setup['profile']}]")
        print(f"H4: bias = {setup['bias']}, zone = {setup['zone']}")
        print(f"H1 OB: {setup['order_block_zone']}")
        print(
            f"Entry: {setup['entry']:.2f}, Stop: {setup['stop']:.2f}, "
            f"Target: {setup['target']:.2f}, RR = {setup['rr_value']:.2f}"
        )
        print(f"Причина: {setup['reason']}")
        rel_vol = setup.get("rel_volume_h1")
        if rel_vol is not None:
            print(f"Объём H1: x{rel_vol:.2f} от среднего")


def scan_once(symbols: list[str] | None = None) -> list[dict]:
    """Один проход по списку монет. Возвращает найденные сетапы."""
    symbols = symbols or SYMBOLS
    setups: list[dict] = []

    for symbol in symbols:
        try:
            df_h4 = get_ohlcv(symbol, CONTEXT_TF)
            df_h1 = get_ohlcv(symbol, ENTRY_TF)

            h4 = analyze_smc(df_h4, CONTEXT_TF)
            h1 = analyze_smc(df_h1, ENTRY_TF)

            symbol_setups: list[dict] = []
            volume_metrics = _calc_h1_volume(df_h1)
            for check_fn in (check_long_setup, check_short_setup):
                setup = check_fn(symbol, h4, h1)
                if setup:
                    setup.update(volume_metrics)
                    _apply_volume_impact(setup)
                    symbol_setups.append(setup)
                    setups.append(setup)

            _log_symbol(symbol, h4, h1, symbol_setups)
            print()

        except Exception as exc:
            print(f"[{symbol}] Ошибка: {exc}")
            print()

    return setups


def run_scanner(loop: bool = True) -> None:
    """Запускает сканер в цикле или один раз."""
    interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
    dry_run = DRY_RUN

    print("SMC Market Scanner запущен")
    print(f"Биржа: {os.getenv('EXCHANGE', 'binance')}")
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
