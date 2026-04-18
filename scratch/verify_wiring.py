import pandas as pd
import numpy as np
from portfolio.manager import PortfolioManager
# Minimal synthetic BTC frame long enough for regime detection (>=210 rows).
n = 300
rng = pd.date_range("2025-01-01", periods=n, freq="1h")
close = np.linspace(40000, 50000, n)
df = pd.DataFrame({
    "timestamp": rng,
    "open":  close,
    "high":  close * 1.002,
    "low":   close * 0.998,
    "close": close,
    "volume": 100.0,
}).set_index("timestamp")
pm = PortfolioManager(total_capital=10_000)
pm.initialize(initial_btc_df=df)
assert len(pm.kelly_profiles) == 10, pm.kelly_profiles
assert pm._last_kelly_regime is not None
assert pm._candles_since_kelly_rebuild == 0
for name, p in pm.kelly_profiles.items():
    assert p.half_kelly >= 0, (name, p)
print("OK strategies:", list(pm.kelly_profiles.keys()))
print("regime:", pm._last_kelly_regime)
