"""
SMC Strategy v2 - Финансовый анализатор и отчётчик

Этот скрипт содержит полный анализ стратегии SMC с расчётами в долларах
для депозита $10,000 и риском $25 за сделку.

Автор: SMC Scanner v2
Дата: 27.07.2026
"""

from dataclasses import dataclass
from typing import Dict, List

# ============================================================================
# КОНФИГУРАЦИЯ СТРАТЕГИИ
# ============================================================================

STRATEGY_CONFIG = {
    # Таймфреймы
    "BIAS_TF": "H4",
    "STRUCTURE_TF": "H1",
    "TRIGGER_TF": "M15",
    
    # ATR и стоп
    "ATR_PERIOD": 14,
    "ATR_MULT": 2.0,
    "MIN_STOP_DISTANCE_PCT": 0.003,
    
    # Риск-менеджмент
    "FIXED_RR": 2.5,
    "RISK_PCT_PER_TRADE": 0.0025,  # 0.25%
    "BREAKEVEN_TRIGGER_R": 0.0,
    
    # Фильтры
    "VOLUME_SPIKE_THRESHOLD": 1.5,
    "MIN_PROFILE_SCORE": 0,
    "STRUCTURE_ONLY_FVG": True,
    
    # Параметры AMD-свипа
    "SWEEP_LOOKBACK": 10,
    "SWEEP_REVERSAL_BARS": 2,
}

# ============================================================================
# РЕЗУЛЬТАТЫ БЭКТЕСТОВ
# ============================================================================

@dataclass
class BacktestResults:
    """Результаты бэктеста одной пары"""
    symbol: str
    total_setups: int
    wins: int
    losses: int
    breakevens: int
    winrate: float
    avg_rr: float
    total_r: float
    max_dd_r: float
    period_label: str


BACKTEST_DATA = {
    "BTCUSDT": BacktestResults(
        symbol="BTCUSDT",
        total_setups=2002,
        wins=101,
        losses=50,
        breakevens=1851,
        winrate=66.9,
        avg_rr=2.50,
        total_r=202.50,
        max_dd_r=4.00,
        period_label="~1.5 года"
    ),
    "ETHUSDT": BacktestResults(
        symbol="ETHUSDT",
        total_setups=2127,
        wins=106,
        losses=62,
        breakevens=1957,
        winrate=63.1,
        avg_rr=2.50,
        total_r=203.00,
        max_dd_r=4.00,
        period_label="~2 года"
    ),
    "XRPUSDT": BacktestResults(
        symbol="XRPUSDT",
        total_setups=2266,
        wins=120,
        losses=43,
        breakevens=2103,
        winrate=73.6,
        avg_rr=2.50,
        total_r=257.00,
        max_dd_r=3.00,
        period_label="~2 года"
    ),
}


# ============================================================================
# ФИНАНСОВЫЕ РАСЧЁТЫ
# ============================================================================

class FinancialCalculator:
    """Калькулятор финансовых показателей стратегии"""
    
    def __init__(self, initial_deposit: float = 10000, risk_per_trade: float = 25):
        self.initial_deposit = initial_deposit
        self.risk_per_trade = risk_per_trade
        self.risk_pct = risk_per_trade / initial_deposit
    
    def calculate_results(self, backtest: BacktestResults) -> Dict:
        """Рассчитывает финансовые показатели для пары"""
        
        profit_usd = backtest.total_r * self.risk_per_trade
        max_dd_usd = backtest.max_dd_r * self.risk_per_trade
        final_balance = self.initial_deposit + profit_usd
        roi = (profit_usd / self.initial_deposit) * 100
        
        profit_factor = backtest.wins / backtest.losses if backtest.losses > 0 else float('inf')
        
        avg_trade_pnl = profit_usd / backtest.total_setups if backtest.total_setups > 0 else 0
        
        return {
            "symbol": backtest.symbol,
            "initial_deposit": self.initial_deposit,
            "risk_per_trade": self.risk_per_trade,
            "risk_pct": self.risk_pct * 100,
            
            # Статистика
            "total_setups": backtest.total_setups,
            "wins": backtest.wins,
            "losses": backtest.losses,
            "winrate": backtest.winrate,
            
            # Финансовые показатели
            "total_pnl_r": backtest.total_r,
            "profit_usd": profit_usd,
            "max_dd_usd": max_dd_usd,
            "final_balance": final_balance,
            "roi_pct": roi,
            "profit_factor": profit_factor,
            "avg_pnl_per_trade": avg_trade_pnl,
            
            # Показатели качества
            "period": backtest.period_label,
        }


# ============================================================================
# ФОРМАТИРОВАНИЕ И ВЫВОД
# ============================================================================

def format_currency(value: float) -> str:
    """Форматирует значение как валюту"""
    return f"${value:,.2f}"


def format_percent(value: float) -> str:
    """Форматирует значение как процент"""
    return f"{value:+.2f}%"


def format_multiplier(value: float) -> str:
    """Форматирует множитель"""
    if value == float('inf'):
        return "∞"
    return f"{value:.2f}x"


def print_header(title: str, width: int = 80):
    """Печатает заголовок раздела"""
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)


def print_single_pair_report(results: Dict):
    """Выводит отчёт по одной паре"""
    print(f"\n{'─' * 80}")
    print(f"  {results['symbol'].upper()}")
    print(f"{'─' * 80}")
    
    print(f"\n  Торговая статистика:")
    print(f"    Всего сетапов:        {results['total_setups']:,}")
    print(f"    Выигрышей (Win):      {results['wins']:,}")
    print(f"    Проигрышей (Loss):    {results['losses']:,}")
    print(f"    Винрейт:              {results['winrate']:.1f}%")
    print(f"    Профит фактор:        {format_multiplier(results['profit_factor'])}")
    
    print(f"\n  Финансовые показатели:")
    print(f"    Начальный баланс:     {format_currency(results['initial_deposit'])}")
    print(f"    Риск на сделку:       {format_currency(results['risk_per_trade'])} ({results['risk_pct']:.2f}%)")
    print(f"    Общая прибыль:        {format_currency(results['profit_usd'])}")
    print(f"    Макс просадка:        {format_currency(results['max_dd_usd'])}")
    print(f"    Финальный баланс:     {format_currency(results['final_balance'])}")
    print(f"    ROI:                  {format_percent(results['roi_pct'])}")
    print(f"    Сред. P&L за сделку:  {format_currency(results['avg_pnl_per_trade'])}")
    
    print(f"\n  Период тестирования: {results['period']}")


def print_consolidated_report(all_results: List[Dict]):
    """Выводит консолидированный отчёт по всем парам"""
    print_header("КОНСОЛИДИРОВАННЫЙ ФИНАНСОВЫЙ ОТЧЁТ (3 ПАРЫ)", 100)
    
    # Таблица
    print(f"\n{'Пара':<12} {'Сетапы':>8} {'Win':>5} {'Loss':>5} {'Винрейт':>8} {'PF':>6} {'Прибыль':>12} {'ROI':>8} {'Max DD':>10}")
    print("─" * 100)
    
    total_profit = 0
    total_setups = 0
    total_wins = 0
    total_losses = 0
    
    for res in all_results:
        print(
            f"{res['symbol']:<12} "
            f"{res['total_setups']:>8,} "
            f"{res['wins']:>5} "
            f"{res['losses']:>5} "
            f"{res['winrate']:>7.1f}% "
            f"{format_multiplier(res['profit_factor']):>6} "
            f"{format_currency(res['profit_usd']):>12} "
            f"{format_percent(res['roi_pct']):>8} "
            f"{format_currency(res['max_dd_usd']):>10}"
        )
        
        total_profit += res['profit_usd']
        total_setups += res['total_setups']
        total_wins += res['wins']
        total_losses += res['losses']
    
    print("─" * 100)
    
    # Итого
    avg_winrate = (total_wins / (total_wins + total_losses)) * 100 if (total_wins + total_losses) > 0 else 0
    avg_pf = total_wins / total_losses if total_losses > 0 else float('inf')
    avg_roi = (total_profit / (10000 * 3)) * 100  # Для трёх $10k депозитов
    
    print(
        f"{'ИТОГО':<12} "
        f"{total_setups:>8,} "
        f"{total_wins:>5} "
        f"{total_losses:>5} "
        f"{avg_winrate:>7.1f}% "
        f"{format_multiplier(avg_pf):>6} "
        f"{format_currency(total_profit):>12} "
        f"{format_percent(avg_roi):>8} "
        f"─"
    )


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    """Основная функция анализа"""
    
    print_header("SMC STRATEGY v2 - ФИНАНСОВЫЙ ОТЧЁТ", 100)
    print("\nДепозит: $10,000 | Риск на сделку: $25 (0.25%)\n")
    
    # Калькулятор
    calc = FinancialCalculator(initial_deposit=10000, risk_per_trade=25)
    
    # Рассчитываем результаты для каждой пары
    all_results = []
    for symbol, backtest in BACKTEST_DATA.items():
        results = calc.calculate_results(backtest)
        all_results.append(results)
        print_single_pair_report(results)
    
    # Консолидированный отчёт
    print_consolidated_report(all_results)
    
    # Рекомендации
    print_header("РЕКОМЕНДАЦИИ ПО ИСПОЛЬЗОВАНИЮ", 100)
    print("""
1. ВЫБОР ПАР:
   • ⭐ XRP показал лучший результат (73.6% винрейт, +64.25% ROI)
   • ✅ BTC стабилен (66.9% винрейт, +50.62% ROI)
   • ⚠️  ETH слабее (63.1% винрейт, +50.75% ROI)
   
2. РАЗМЕР ПОЗИЦИИ:
   • Начните с одной пары (рекомендуем XRP)
   • $25 риск за сделку = 1 минилот для большинства пар
   • Увеличивайте постепенно (по 10% депозита в месяц)
   
3. УПРАВЛЕНИЕ РИСКАМИ:
   • Следите, чтобы просадка не превышала -10% депозита
   • Если просадка > 5%, переходите на микро-лоты
   • Ведите дневник всех сделок
   
4. МОНИТОРИНГ:
   • Винрейт должен быть > 60% в реальной торговле
   • Пересчитывайте стратегию ежемесячно
   • Обновляйте параметры если винрейт упадёт < 55%
   
5. ПОРТФЕЛЬНЫЙ ПОДХОД:
   • Если торговать все 3 пары: ~+55% ROI за период
   • Макс просадка: ~$100 одновременно
   • Диверсификация снижает риск
    """)
    
    # Параметры
    print_header("ТЕКУЩИЕ ПАРАМЕТРЫ СТРАТЕГИИ", 100)
    for key, value in sorted(STRATEGY_CONFIG.items()):
        print(f"  {key:<30} = {value}")
    
    print("\n")


if __name__ == "__main__":
    main()
