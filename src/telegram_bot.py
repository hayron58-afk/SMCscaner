"""Отправка алертов в Telegram."""

from __future__ import annotations

import os

import requests
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(dotenv_path=ENV_PATH)


def _format_alert_message(setup: dict) -> str:
    direction = setup["direction"]
    emoji = "🟢" if direction == "LONG" else "🔴"
    profile = setup.get("profile", "WATCH")
    pos_sizes = setup.get("position_sizes", {})
    reason = setup.get("reason", "")

    rel_vol = setup.get("rel_volume")
    vol_line = ""
    if rel_vol is not None:
        if rel_vol >= 1.5:
            vol_line = f"\nОбъём: выше среднего (x{rel_vol:.2f}) — движение подтверждено."
        elif rel_vol <= 0.5:
            vol_line = f"\nОбъём: ниже среднего (x{rel_vol:.2f}) — движение может быть слабым."

    bonus_bits = []
    if setup.get("bias_aligned"):
        bonus_bits.append(f"{setup.get('timeframe_context', 'H4')} bias")
    if setup.get("amd_sweep"):
        bonus_bits.append("AMD-свип")
    bonus_line = f"\nБонусы: {', '.join(bonus_bits)}" if bonus_bits else ""

    rm_lines = []
    base_asset = setup["symbol"].replace("USDT", "")
    for deposit, info in pos_sizes.items():
        rm_lines.append(
            f"• депозит {deposit:>6} → риск {info['risk_usd']:.2f}$, "
            f"объём ≈ {info['qty']:.4f} {base_asset}"
        )
    rm_text = "\n".join(rm_lines)

    zone_kind = setup.get("zone_kind", "OB")
    structure_tf = setup.get("timeframe_structure", setup.get("timeframe_context", ""))

    return (
        f"{emoji} {setup['symbol']} [{profile}]\n\n"
        f"{setup['timeframe_entry']} вход / {structure_tf} {zone_kind}\n"
        f"Zone: {setup['order_block_zone']}\n"
        f"Entry: {setup['entry']:.4f}\n"
        f"Stop:  {setup['stop']:.4f}\n"
        f"Target:{setup['target']:.4f}\n"
        f"RR ≈ {setup['rr_value']:.2f}\n\n"
        f"Причина: {reason}{bonus_line}{vol_line}\n\n"
        f"Есть вариант сделки.\n"
        f"Монета: {setup['symbol']}, направление: {direction}.\n"
        f"Условия: {reason}.\n\n"
        f"Риск-менеджмент при риске 0.25%:\n"
        f"{rm_text}\n\n"
        f"Проверь график. Вход только при подтверждении свечой."
    )


def send_alert(setup: dict) -> bool:
    """
    Отправляет алерт о сетапе в Telegram.

    Читает TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID из окружения.
    Вызывается только когда DRY_RUN=false (логика в scanner.py).

    Returns:
        True если отправка успешна, False при ошибке.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Ошибка: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
        print(f"Ожидаемый путь к .env: {ENV_PATH}")
        print(f"Файл существует: {os.path.isfile(ENV_PATH)}")
        return False

    message = _format_alert_message(setup)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        print("Telegram: сообщение отправлено.")
        return True
    except requests.RequestException as exc:
        print(f"Ошибка отправки в Telegram: {exc}")
        return False


if __name__ == "__main__":
    from src.rr_calc import calc_position_sizes

    test_setup = {
        "symbol": "TESTUSDT",
        "direction": "LONG",
        "timeframe_context": "H4",
        "timeframe_structure": "H1",
        "timeframe_entry": "M15",
        "bias": "bullish",
        "bias_aligned": True,
        "zone": "discount",
        "order_block_zone": "OB 100.00 – 101.00",
        "zone_kind": "OB",
        "amd_sweep": True,
        "entry": 100.50,
        "stop": 99.80,
        "target": 102.25,
        "rr_value": 2.5,
        "profile": "GRAIL",
        "reason": "H1 OB + M15 реакция + H4 bias совпадает + AMD-свип ликвидности + повышенный объём",
        "rel_volume": 1.8,
        "avg_volume": 1000.0,
        "last_volume": 1800.0,
    }
    test_setup["position_sizes"] = calc_position_sizes(
        entry=test_setup["entry"],
        stop=test_setup["stop"],
        risk_pct=0.0025,
    )
    send_alert(test_setup)
