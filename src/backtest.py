"""Бэктест SMC-стратегии v2 (три таймфрейма) на исторических данных.

Логика полностью переиспользуется из src.scanner — этот файл не содержит
своей копии стратегии: check_setup, _collect_reaction_zones,
_find_zone_near_price, _check_reaction берутся оттуда же, что использует
боевой сканер.

Вход в CSV — только один файл, M15 (15-минутные свечи):
    timestamp, open, high, low, close, volume

H1 и H4 строятся РЕСЕМПЛИНГОМ этих же M15-данных (как и в боевом режиме
через data_source.get_ohlcv с TIMEFRAME_MAP, только тут без обращения к
бирже — сами агрегируем локально).

Важная оптимизация: structure (H1) и bias (H4) анализ пересчитывается не
на каждом M15-баре, а только когда закрывается новый H1/H4-бар — иначе
дорогой smartmoneyconcepts-анализ пришлось бы гонять ~35000 раз в год
вместо ~8760/2190. Это одновременно и быстрее, и реалистичнее: в боевом
режиме сканер тоже не видит H1/H4 чаще, чем эти свечи реально закрываются.

Критерий win/loss/timeout — тот же, что в v1: смотрим вперёд по M15-барам,
кто из stop/target сработал раньше; если задеты оба в одном баре —
консервативно loss.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from statistics import median

import pandas as pd

from src.scanner import (
    ATR_PERIOD,
    BIAS_TF,
    FIXED_RR,
    STRUCTURE_TF,
    TRIGGER_TF,
    _check_reaction,
    _collect_reaction_zones,
    _find_zone_near_price,
    check_setup,
)
from src.indicators import calculate_atr
from src.telegram_bot import _format_alert_message, send_alert

# Сколько последних завершённых H1/H4-баров показываем анализатору за раз.
STRUCTURE_WINDOW_H1_BARS = 400  # ~16.6 дней
BIAS_WINDOW_H4_BARS = 200       # ~33 дня

# Минимум завершённых баров, чтобы swing/BOS на H1 и H4 были устойчивы
# (SWING_LENGTH=20 в smc_analyzer.py + запас).
MIN_STRUCTURE_BARS = 30
MIN_BIAS_BARS = 30

# Скользящее окно M15-истории для ATR/AMD-свипа на триггерном таймфрейме.
TRIGGER_WINDOW_BARS = 60  # 15 часов — с запасом для ATR_PERIOD=14 + свип

MAX_LOOKAHEAD_BARS = 480  # ~5 дней в M15-барах — горизонт поиска исхода


@dataclass
class BacktestResult:
    setups: list[dict]
    outcomes: list[str]
    pnls: list[float]
    stats: dict


def _load_m15_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"В CSV отсутствуют колонки: {sorted(missing)}")

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    if df["timestamp"].isna().any():
        raise ValueError("Не удалось распарсить часть значений timestamp")

    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp")
    df = df.set_index("timestamp")
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _resample(df_m15: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Строит старший TF из M15, отбрасывая незавершённый последний бар
    (иначе бэктест "видел" бы контекст раньше, чем он реально сформировался
    бы в живом режиме — lookahead bias)."""
    try:
        grouped = df_m15.resample(rule, label="left", closed="left")
    except ValueError:
        grouped = df_m15.resample(rule.upper(), label="left", closed="left")

    agg = grouped.agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()

    if agg.empty:
        return agg

    bar_seconds = pd.Timedelta(rule).total_seconds()
    last_start = agg.index[-1]
    bars_in_last = df_m15.loc[last_start:].shape[0]
    expected_bars = bar_seconds / (15 * 60)
    if bars_in_last < expected_bars:
        agg = agg.iloc[:-1]

    return agg


def _completed_bars(resampled: pd.DataFrame, current_ts: pd.Timestamp, bar_delta: pd.Timedelta) -> pd.DataFrame:
    """Только те бары старшего TF, которые реально ЗАКРЫЛИСЬ к current_ts."""
    return resampled[resampled.index + bar_delta <= current_ts]


def _simulate_outcome(
    df_m15_full: pd.DataFrame,
    entry_position: int,
    setup: dict,
    max_bars: int,
    breakeven_trigger_r: float = 0.0,
) -> tuple[str, float]:
    """Симулирует исход сделки с переносом стопа в безубыток.

    breakeven_trigger_r — доля от stop_distance, на которую цена должна
    пройти в прибыль, прежде чем стоп переносится в точку входа.
    0.0 = перенос происходит, как только сделка вообще стала прибыльной
    (цена коснулась уровня чуть лучше entry) — так, как попросили.

    Важная оговорка по интрабарной неопределённости: перенос стопа
    применяется начиная СО СЛЕДУЮЩЕГО бара после того, как сработал
    триггер, а не задним числом внутри того же бара — по одной свече
    (только OHLC, без тиков) нельзя понять, в каком порядке цена внутри
    бара прошла уровни. Это консервативное допущение: не даёт переносу
    "задним числом" спасти бар, где стоп и триггер задеты одновременно.

    Возвращает (outcome, pnl_r), где outcome — "win"/"loss"/"breakeven"/"timeout".
    breakeven даёт pnl_r=0.0 (вышли по нулям, а не по полному стопу).
    """
    direction = setup["direction"]
    entry = setup["entry"]
    stop = setup["stop"]
    target = setup["target"]
    stop_distance = abs(entry - stop)

    if direction == "LONG":
        breakeven_trigger_price = entry + stop_distance * breakeven_trigger_r
    else:
        breakeven_trigger_price = entry - stop_distance * breakeven_trigger_r

    current_stop = stop
    moved_to_breakeven = False

    start = entry_position + 1
    end = min(start + max_bars, len(df_m15_full))
    window = df_m15_full.iloc[start:end]

    for _, bar in window.iterrows():
        hi, lo = bar["high"], bar["low"]

        if direction == "LONG":
            hit_stop = lo <= current_stop
            hit_target = hi >= target
        else:
            hit_stop = hi >= current_stop
            hit_target = lo <= target

        if hit_stop:
            if moved_to_breakeven:
                return "breakeven", 0.0
            return "loss", -1.0
        if hit_target:
            return "win", setup["rr_value"]

        if not moved_to_breakeven:
            if direction == "LONG":
                reached_trigger = hi >= breakeven_trigger_price
            else:
                reached_trigger = lo <= breakeven_trigger_price
            if reached_trigger:
                current_stop = entry
                moved_to_breakeven = True

    return "timeout", 0.0


def _new_funnel() -> dict:
    return {
        "trigger_bars": 0,
        "long_zones_found": 0,
        "long_zone_near_price": 0,
        "long_reaction_ok": 0,
        "long_setup_ok": 0,
        "short_zones_found": 0,
        "short_zone_near_price": 0,
        "short_reaction_ok": 0,
        "short_setup_ok": 0,
        "examples": [],
    }


def _update_funnel(funnel: dict, structure_analysis: dict, df_trigger: pd.DataFrame) -> None:
    funnel["trigger_bars"] += 1
    price = float(df_trigger["close"].iloc[-1])
    last_bar = df_trigger.iloc[-1]

    for side in ("long", "short"):
        direction = "bullish" if side == "long" else "bearish"
        zones = _collect_reaction_zones(structure_analysis, direction)
        if not zones:
            continue
        funnel[f"{side}_zones_found"] += 1

        zone = _find_zone_near_price(zones, price)
        if zone is None:
            if len(funnel["examples"]) < 5:
                funnel["examples"].append(
                    (side, price, [(z["kind"], z["top"], z["bottom"], z["index"]) for z in zones])
                )
            continue
        funnel[f"{side}_zone_near_price"] += 1

        if not _check_reaction(last_bar, direction, zone):
            continue
        funnel[f"{side}_reaction_ok"] += 1

        atr = calculate_atr(df_trigger, period=ATR_PERIOD).iloc[-1]
        if not pd.isna(atr) and atr > 0:
            funnel[f"{side}_setup_ok"] += 1


def _print_funnel(funnel: dict) -> None:
    print("=== Диагностика воронки v2 (--debug) ===")
    print(f"Проанализировано M15-баров: {funnel['trigger_bars']}")
    for side in ("long", "short"):
        print(f"-- {side.upper()} --")
        print(f"  есть OB/FVG нужного направления на {STRUCTURE_TF}: {funnel[f'{side}_zones_found']}")
        print(f"  цена внутри одной из зон: {funnel[f'{side}_zone_near_price']}")
        print(f"  реакция свечой на {TRIGGER_TF}: {funnel[f'{side}_reaction_ok']}")
        print(f"  ATR доступен -> сетап создан: {funnel[f'{side}_setup_ok']}")

    if funnel["examples"]:
        print("\nПримеры промахов (цена vs все доступные зоны того же направления):")
        for side, price, zones in funnel["examples"]:
            print(f"  [{side}] цена = {price:.4f}")
            for kind, top, bottom, idx in zones:
                dist_pct = (min(abs(price - top), abs(price - bottom)) / price) * 100
                print(f"      {kind} idx={idx}: {bottom:.4f} – {top:.4f} (граница в {dist_pct:.2f}% от цены)")
    print()


def run_backtest(
    csv_path: str,
    symbol: str,
    max_lookahead_bars: int = MAX_LOOKAHEAD_BARS,
    breakeven_trigger_r: float = 0.0,
    debug: bool = False,
) -> BacktestResult:
    from src.scanner import find_setups  # локальный импорт — избегаем цикличности при линтерах

    df_m15_full = _load_m15_csv(csv_path)
    resampled_1h = _resample(df_m15_full, "1h")
    resampled_4h = _resample(df_m15_full, "4h")

    delta_1h = pd.Timedelta("1h")
    delta_4h = pd.Timedelta("4h")

    # Ищем самую раннюю точку, где уже есть достаточно завершённых H1 и H4
    # баров для устойчивого анализа — это и есть точка старта бэктеста.
    warmup_ts = None
    for ts in df_m15_full.index:
        c1h = _completed_bars(resampled_1h, ts, delta_1h)
        c4h = _completed_bars(resampled_4h, ts, delta_4h)
        if len(c1h) >= MIN_STRUCTURE_BARS and len(c4h) >= MIN_BIAS_BARS:
            warmup_ts = ts
            break

    if warmup_ts is None:
        raise ValueError(
            "Недостаточно данных: не набралось нужное количество завершённых "
            f"H1 ({MIN_STRUCTURE_BARS}) / H4 ({MIN_BIAS_BARS}) баров ни на одном M15-баре."
        )

    start_pos = df_m15_full.index.get_loc(warmup_ts)

    setups: list[dict] = []
    outcomes: list[str] = []
    pnls: list[float] = []
    last_setup_seen: dict[str, tuple] = {}  # direction -> (zone_kind, zone_top, zone_bottom)
    funnel = _new_funnel() if debug else None

    cached_1h_last_ts = None
    cached_4h_last_ts = None
    structure_analysis = None
    bias_analysis = None

    from src.smc_analyzer import analyze_smc

    for i in range(start_pos, len(df_m15_full)):
        current_ts = df_m15_full.index[i]

        c1h = _completed_bars(resampled_1h, current_ts, delta_1h).tail(STRUCTURE_WINDOW_H1_BARS)
        c4h = _completed_bars(resampled_4h, current_ts, delta_4h).tail(BIAS_WINDOW_H4_BARS)

        if len(c1h) < MIN_STRUCTURE_BARS or len(c4h) < MIN_BIAS_BARS:
            continue

        if c1h.index[-1] != cached_1h_last_ts:
            structure_analysis = analyze_smc(c1h.reset_index(drop=True), STRUCTURE_TF)
            cached_1h_last_ts = c1h.index[-1]

        if c4h.index[-1] != cached_4h_last_ts:
            bias_analysis = analyze_smc(c4h.reset_index(drop=True), BIAS_TF)
            cached_4h_last_ts = c4h.index[-1]

        trigger_start = max(0, i + 1 - TRIGGER_WINDOW_BARS)
        df_trigger = df_m15_full.iloc[trigger_start : i + 1].reset_index(drop=True)

        if debug:
            _update_funnel(funnel, structure_analysis, df_trigger)

        for direction in ("bullish", "bearish"):
            setup = check_setup(symbol, direction, bias_analysis, structure_analysis, df_trigger)
            if setup is None:
                continue

            key = (setup["zone_kind"], setup["order_block_zone"])
            if last_setup_seen.get(direction) == key:
                continue  # та же зона, что и в прошлый раз — не новый сигнал
            last_setup_seen[direction] = key

            outcome, pnl_r = _simulate_outcome(
                df_m15_full, i, setup, max_lookahead_bars, breakeven_trigger_r
            )
            setups.append(setup)
            outcomes.append(outcome)
            pnls.append(pnl_r)

    if debug:
        _print_funnel(funnel)

    stats = _compute_stats(setups, outcomes, pnls)
    return BacktestResult(setups=setups, outcomes=outcomes, pnls=pnls, stats=stats)


def _compute_stats(setups: list[dict], outcomes: list[str], pnls: list[float]) -> dict:
    total = len(setups)
    wins = outcomes.count("win")
    losses = outcomes.count("loss")
    breakevens = outcomes.count("breakeven")
    timeouts = outcomes.count("timeout")
    decided = wins + losses  # breakeven — это "вышли по нулям", не считаем ни победой, ни поражением

    win_rate = wins / decided if decided else None
    avg_rr = sum(s["rr_value"] for s in setups) / total if total else 0.0
    median_rr = median(s["rr_value"] for s in setups) if setups else 0.0

    # В equity идут все закрытые сделки (win/loss/breakeven), timeout исключается —
    # по нему неизвестен реальный исход в горизонте бэктеста.
    r_multiples = [
        pnl for outcome, pnl in zip(outcomes, pnls) if outcome != "timeout"
    ]

    equity = [0.0]
    for r in r_multiples:
        equity.append(equity[-1] + r)

    peak = equity[0]
    max_dd = 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    return {
        "total_setups": total,
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "timeouts": timeouts,
        "win_rate": win_rate,
        "avg_rr": avg_rr,
        "median_rr": median_rr,
        "total_r": sum(r_multiples),
        "median_r": median(r_multiples) if r_multiples else 0.0,
        "max_drawdown_r": max_dd,
    }


def _segment_win_rates(setups: list[dict], outcomes: list[str], classify) -> dict:
    groups: dict = {}
    for setup, outcome in zip(setups, outcomes):
        label = classify(setup)
        g = groups.setdefault(label, {"wins": 0, "losses": 0, "other": 0})
        if outcome == "win":
            g["wins"] += 1
        elif outcome == "loss":
            g["losses"] += 1
        else:
            g["other"] += 1  # breakeven или timeout — ни победа, ни поражение

    result = {}
    for label, g in groups.items():
        decided = g["wins"] + g["losses"]
        result[label] = {
            "n": g["wins"] + g["losses"] + g["other"],
            "win_rate": (g["wins"] / decided) if decided else None,
            "wins": g["wins"],
            "losses": g["losses"],
        }
    return result


def _print_segments(setups: list[dict], outcomes: list[str]) -> None:
    if not setups:
        return

    breakeven = 1 / (1 + FIXED_RR)
    print(f"=== Разбивка по бонус-флагам (порог безубыточности при RR={FIXED_RR}: {breakeven * 100:.1f}%) ===")

    segments = [
        ("H4 bias совпадает", lambda s: "да" if s.get("bias_aligned") else "нет"),
        ("AMD-свип", lambda s: "да" if s.get("amd_sweep") else "нет"),
        ("Объём (spike >=1.5x)", lambda s: "да" if (s.get("rel_volume") or 0) >= 1.5 else "нет"),
        ("Тип зоны", lambda s: s.get("zone_kind", "?")),
        ("Профиль", lambda s: s.get("profile", "?")),
    ]

    for title, classify in segments:
        segs = _segment_win_rates(setups, outcomes, classify)
        print(f"-- {title} --")
        for label, s in sorted(segs.items()):
            if s["win_rate"] is None:
                print(f"  {label}: n={s['n']}, винрейт=н/д (нет закрытых)")
                continue
            delta = (s["win_rate"] - breakeven) * 100
            marker = " <-- выше безубыточности" if delta > 2 else ""
            print(
                f"  {label}: n={s['n']}, винрейт={s['win_rate'] * 100:.1f}% "
                f"(W{s['wins']}/L{s['losses']}, {delta:+.1f}пп от порога){marker}"
            )
    print()


def _print_report(result: BacktestResult, symbol: str, max_examples: int = 3) -> None:
    stats = result.stats
    print(f"=== Бэктест v2 {symbol} ===")
    print(f"Найдено сетапов: {stats['total_setups']}")
    print(f"  Win: {stats['wins']}  Loss: {stats['losses']}  Breakeven: {stats['breakevens']}  Timeout: {stats['timeouts']}")
    if stats["win_rate"] is not None:
        print(f"Винрейт (без timeout): {stats['win_rate'] * 100:.1f}%")
    else:
        print("Винрейт: нет закрытых сделок в пределах горизонта")
    print(f"Средний RR: {stats['avg_rr']:.2f}  |  Медианный RR: {stats['median_rr']:.2f}")
    print(f"Суммарный условный PnL: {stats['total_r']:.2f}R  |  Медианный PnL сделки: {stats['median_r']:+.2f}R")
    print(f"Максимальная просадка: {stats['max_drawdown_r']:.2f}R")
    print()

    if not result.setups:
        return

    print(f"Примеры сетапов (первые {max_examples}, то же сообщение, что в Telegram):")
    for setup, outcome in list(zip(result.setups, result.outcomes))[:max_examples]:
        print("-" * 60)
        print(_format_alert_message(setup))
        print(f">>> Исход в бэктесте: {outcome.upper()}")
    print("-" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Бэктест SMC-стратегии v2 (три таймфрейма)")
    parser.add_argument("--data", required=True, help="Путь к CSV с историческими M15-свечами")
    parser.add_argument("--symbol", required=True, help="Символ, например BTCUSDT")
    parser.add_argument(
        "--max-lookahead",
        type=int,
        default=MAX_LOOKAHEAD_BARS,
        help="Горизонт поиска исхода сделки, в M15-барах",
    )
    parser.add_argument(
        "--breakeven-trigger-r",
        type=float,
        default=0.0,
        help="Доля stop_distance в плюс, после которой стоп переносится в безубыток "
        "(0.0 = сразу как только сделка вышла в плюс; 1.0 = только после +1R)",
    )
    parser.add_argument(
        "--send-alerts",
        action="store_true",
        help="Дополнительно отправить каждый найденный сетап в Telegram через send_alert",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Показать воронку: зоны / попадание цены / реакция / ATR",
    )
    args = parser.parse_args()

    result = run_backtest(
        csv_path=args.data,
        symbol=args.symbol,
        max_lookahead_bars=args.max_lookahead,
        breakeven_trigger_r=args.breakeven_trigger_r,
        debug=args.debug,
    )

    _print_report(result, args.symbol)
    _print_segments(result.setups, result.outcomes)

    if args.send_alerts:
        print(f"--send-alerts: отправляю {len(result.setups)} алертов в Telegram...")
        for setup in result.setups:
            send_alert(setup)


if __name__ == "__main__":
    main()
