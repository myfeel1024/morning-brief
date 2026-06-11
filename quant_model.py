"""
LightGBM 팩터 랭킹 모델
과거 팩터 → 미래 수익률 예측 학습 → 현재 종목 랭킹
"""

from pathlib import Path

import numpy as np
import pandas as pd

MODEL_PATH = Path(__file__).resolve().parent / "quant_lgb_model.txt"

FEATURE_COLS = ["mom1m", "mom3m", "mom6m", "ma_cross", "rsi_rev"]

try:
    import lightgbm as lgb
    LGB_OK = True
except ImportError:
    LGB_OK = False


# ── 학습 데이터 생성 ──────────────────────────────────────────

def build_dataset(
    prices: pd.DataFrame,
    forward_days: int = 21,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    월별 스냅샷으로 (팩터, 미래 수익률) 쌍 생성.
    팩터: mom1m/3m/6m, MA크로스, RSI 역방향
    레이블: 21거래일 후 수익률 (백분위 랭크)
    """
    from quant_engine import _momentum, _rsi, _ma_cross, _rank

    X_rows, y_vals = [], []

    # 월말 스냅샷 날짜
    snap_dates = pd.date_range(
        prices.index[70], prices.index[-(forward_days + 1)], freq="ME"
    )

    for snap in snap_dates:
        # 해당 시점까지의 가격
        hist = prices.loc[:snap]
        if len(hist) < 70:
            continue

        # 팩터 계산
        factors = {}
        for name, w in [("mom1m", 21), ("mom3m", 63), ("mom6m", 126)]:
            s = _momentum(hist, w)
            if not s.empty:
                factors[name] = _rank(s)

        mac = _ma_cross(hist)
        if not mac.empty:
            factors["ma_cross"] = _rank(mac)

        rsi = _rsi(hist)
        if not rsi.empty:
            factors["rsi_rev"] = _rank(-rsi)

        if len(factors) < 3:
            continue

        df_f = pd.DataFrame(factors).dropna(thresh=3)

        # 미래 수익률
        future_idx = prices.index[prices.index > snap]
        if len(future_idx) < forward_days:
            continue
        future_date = future_idx[forward_days - 1]

        for ticker in df_f.index:
            if ticker not in prices.columns:
                continue
            try:
                p0 = prices.loc[snap, ticker]
                p1 = prices.loc[future_date, ticker]
                if p0 > 0 and not np.isnan(p0) and not np.isnan(p1):
                    row = {c: df_f.loc[ticker, c] for c in FEATURE_COLS if c in df_f.columns}
                    row.update({"_snap": snap, "_ticker": ticker})
                    X_rows.append(row)
                    y_vals.append(p1 / p0 - 1)
            except Exception:
                continue

    if not X_rows:
        return pd.DataFrame(), pd.Series(dtype=float)

    X = pd.DataFrame(X_rows).set_index(["_snap", "_ticker"])
    y = pd.Series(y_vals, index=X.index, name="fwd_ret")

    # 그룹별 백분위 랭크로 변환 (절대 수익률 대신 상대 랭킹 학습)
    y_rank = y.groupby(level=0).rank(pct=True)

    X = X.reindex(columns=FEATURE_COLS).fillna(0.5)
    return X, y_rank


# ── 모델 학습 ─────────────────────────────────────────────────

def train(prices: pd.DataFrame, verbose: bool = True) -> object:
    """LightGBM 학습 후 모델 파일 저장"""
    if not LGB_OK:
        raise ImportError("pip install lightgbm")

    if verbose:
        print("  학습 데이터 생성 중 (약 1~2분)...")

    X, y = build_dataset(prices)
    if X.empty:
        raise ValueError("학습 데이터 부족 — 최소 2년치 가격 데이터 필요")

    if verbose:
        print(f"  학습 샘플: {len(X)}개  |  피처: {list(X.columns)}")

    params = {
        "objective":        "regression",
        "metric":           "rmse",
        "learning_rate":    0.05,
        "num_leaves":       31,
        "min_data_in_leaf": 15,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq":     5,
        "lambda_l1":        0.1,
        "verbose":          -1,
    }

    ds    = lgb.Dataset(X, label=y)
    model = lgb.train(params, ds, num_boost_round=300,
                      valid_sets=[ds], callbacks=[lgb.early_stopping(30, verbose=False)])
    model.save_model(str(MODEL_PATH))

    if verbose:
        imp = pd.Series(model.feature_importance(), index=FEATURE_COLS)
        print("  피처 중요도:\n" + "\n".join(f"    {k}: {v}" for k, v in imp.sort_values(ascending=False).items()))
        print(f"  모델 저장 → {MODEL_PATH}")

    return model


# ── 추론 ─────────────────────────────────────────────────────

def load_model():
    if not LGB_OK or not MODEL_PATH.exists():
        return None
    return lgb.Booster(model_file=str(MODEL_PATH))


def predict(prices: pd.DataFrame, model=None) -> pd.Series:
    """
    현재 팩터로 LightGBM 예측 점수 반환 (높을수록 유망).
    모델이 없으면 빈 Series 반환.
    """
    if model is None:
        model = load_model()
    if model is None:
        return pd.Series(dtype=float)

    from quant_engine import _momentum, _rsi, _ma_cross, _rank

    factors = {}
    for name, w in [("mom1m", 21), ("mom3m", 63), ("mom6m", 126)]:
        s = _momentum(prices, w)
        if not s.empty:
            factors[name] = _rank(s)

    mac = _ma_cross(prices)
    if not mac.empty:
        factors["ma_cross"] = _rank(mac)

    rsi = _rsi(prices)
    if not rsi.empty:
        factors["rsi_rev"] = _rank(-rsi)

    df = pd.DataFrame(factors).dropna(thresh=3).fillna(0.5)
    X  = df.reindex(columns=FEATURE_COLS, fill_value=0.5)

    preds = model.predict(X)
    return pd.Series(preds, index=df.index, name="lgb_score").sort_values(ascending=False)


def is_trained() -> bool:
    return MODEL_PATH.exists()


# ── CLI ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from quant_engine import get_prices_batch, days_ago_str, today_str, KOSPI_SAMPLE
    from quant_db import get_prices_cached

    parser = argparse.ArgumentParser()
    parser.add_argument("--train",   action="store_true", help="모델 학습")
    parser.add_argument("--predict", action="store_true", help="현재 종목 예측")
    parser.add_argument("--top",     type=int, default=20)
    args = parser.parse_args()

    tickers = KOSPI_SAMPLE
    prices  = get_prices_cached(tickers, days_ago_str(730), today_str(), verbose=True)

    if args.train:
        print("LightGBM 모델 학습 시작...")
        train(prices)

    if args.predict:
        model  = load_model()
        scores = predict(prices, model)
        if scores.empty:
            print("모델 없음. --train 먼저 실행하세요.")
        else:
            from quant_engine import get_ticker_name
            print(f"\nLightGBM 예측 상위 {args.top}개:")
            for i, (ticker, score) in enumerate(scores.head(args.top).items(), 1):
                print(f"  {i:2}. {get_ticker_name(ticker):<12} ({ticker})  점수 {score:.4f}")
