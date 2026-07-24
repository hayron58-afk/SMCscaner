# SMC Market Scanner

SMC/ICT-сканер для крипторынка (таймфреймы H4/H1) на базе **ccxt** и **smartmoneyconcepts**. Ищет сетапы по структуре (BOS/CHoCH, order block, discount/premium), считает R:R и формирует prop-алерты в Telegram.

**Не торгует** — только анализ и уведомления.

## Установка

```bash
git clone <репозиторий>
cd crypto-smc-scanner
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

На Linux/macOS вместо активации Windows используйте `source .venv/bin/activate`.

## Настройка .env

```bash
cp .env.example .env
# заполнить TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID
# выставить DRY_RUN=false для реальных алертов
```

На Windows:

```bash
copy .env.example .env
```

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен бота от [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | ID чата или канала для алертов |
| `EXCHANGE` | Биржа: `binance`, `bybit`, `bingx` (по умолчанию `binance`) |
| `SCAN_INTERVAL_SECONDS` | Интервал между проходами (по умолчанию 300) |
| `DRY_RUN` | `true` — печатать алерты в консоль без отправки в Telegram |

Публичные свечи работают без API-ключей биржи.

## Запуск

```bash
# один проход
python -m src.scanner --once

# непрерывный сканер
python -m src.scanner
```

## Что содержат алерты

Каждый Telegram-алерт включает:

- **Профиль** — `GRAIL`, `WATCH` или `AGGRESSIVE` (качество и приоритет сетапа)
- **R:R** — соотношение риск/профит по entry, stop и target
- **Причину** — SMC/ICT-контекст (bias, зона premium/discount, order block, объём и т.д.)
- **Расчёт позиции** — для депозитов **1k / 5k / 10k / 50k / 100k** при риске **0.25%** на сделку (сумма риска в USD и объём в базовой монете)

## Логика сетапа (v1)

### Long

- **H4**: bias bullish (последний BOS вверх), цена в discount-зоне
- **H1**: свежий бычий order block, цена внутри OB или у границы
- **R:R** > 2 (entry ≈ текущая цена, stop = низ OB, target = H4-ликвидность или swing high)

### Short

Зеркально: bearish bias, premium-зона, медвежий OB, R:R > 2.

## Структура проекта

```
crypto-smc-scanner/
├── src/
│   ├── data_source.py    # get_ohlcv() — загрузка свечей через ccxt
│   ├── smc_analyzer.py   # analyze_smc() — BOS/CHoCH, OB, premium/discount
│   ├── rr_calc.py        # calculate_rr(), calc_position_sizes()
│   ├── telegram_bot.py   # send_alert()
│   └── scanner.py        # основной цикл
├── .env.example
├── requirements.txt
└── README.md
```

## Монеты по умолчанию

BTC, ETH, SOL, XRP, LINK, DOT (USDT-пары). Список и параметры меняются в `src/scanner.py`.

## Disclaimer

Только для образовательных целей. Не является финансовой рекомендацией. Всегда проверяйте сетап на графике перед решением.
