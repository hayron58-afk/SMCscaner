import pandas as pd
from src.backtest import run_backtest, print_report

df = pd.read_csv("data/BTCUSDT_H1.csv").head(150)
df.to_csv("data/BTCUSDT_H1_small.csv", index=False)
result = run_backtest("data/BTCUSDT_H1_small.csv", "BTCUSDT", "H1")
print_report(result, examples=2)
