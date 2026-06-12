# analysis/historical_engine.py
import os, json, glob
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.app_config import DATA_DIR

HISTORICAL_DIR = os.path.join(DATA_DIR, "historical")
CANDLES_DIR    = os.path.join(DATA_DIR, "candles")


class HistoricalEngine:
    """Загрузка истории свечей и вычисление базовых индикаторов для дашборда."""

    def load_candles(self, pair: str, days: int = 7) -> pd.DataFrame:
        """Загружает свечи из data/candles/{pair}/ за последние N дней."""
        pattern = os.path.join(CANDLES_DIR, pair, f"{pair}_*.csv")
        files = sorted(glob.glob(pattern), reverse=True)[:days]
        if not files:
            return pd.DataFrame()
        frames = []
        for fp in files:
            try:
                df = pd.read_csv(fp)
                frames.append(df)
            except Exception:
                pass
        if not frames:
            return pd.DataFrame()
        result = pd.concat(frames, ignore_index=True)
        result = result.sort_values("timestamp").reset_index(drop=True)
        return result

    def compute_sma(self, df: pd.DataFrame, window: int) -> pd.Series:
        return df["close"].rolling(window, min_periods=1).mean()

    def compute_rsi(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        delta = df["close"].diff()
        gain  = delta.clip(lower=0).rolling(period).mean()
        loss  = (-delta.clip(upper=0)).rolling(period).mean()
        rs    = gain / (loss + 1e-9)
        return 100 - (100 / (1 + rs))

    def get_chart_data(self, pair: str, days: int = 3) -> dict:
        """Возвращает OHLCV + индикаторы для отрисовки графика."""
        df = self.load_candles(pair, days)
        if df.empty:
            return {"pair": pair, "candles": [], "sma100": [], "rsi": []}
        df["sma100"] = self.compute_sma(df, 100)
        df["rsi14"]  = self.compute_rsi(df)
        candles = df[["timestamp","open","high","low","close","volume"]].to_dict("records")
        sma100  = df[["timestamp","sma100"]].dropna().to_dict("records")
        rsi     = df[["timestamp","rsi14"]].dropna().to_dict("records")
        return {"pair": pair, "candles": candles, "sma100": sma100, "rsi": rsi}
