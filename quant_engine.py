"""
KRX 퀀트 투자 엔진
팩터 스코어링 → 신호 생성 → 백테스팅
"""

import warnings
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

try:
    from pykrx import stock as krx
    PYKRX_OK = True
except ImportError:
    PYKRX_OK = False

# ── KOSPI 대표 종목 (pykrx 미설치 시 fallback) ───────────────
KOSPI_SAMPLE = [
    "005930", "000660", "373220", "207940", "005380", "000270",
    "035420", "035720", "051910", "006400", "012330", "005490",
    "068270", "105560", "055550", "086790", "066570", "028260",
    "096770", "034730", "003550", "030200", "017670", "012450",
    "267250", "011070", "247540", "086520", "003670", "036570",
    "259960", "011200", "009150", "352820", "034020", "010130",
    "009540", "329180", "064350", "042660", "015760", "316140",
    "323410", "032830", "000100", "018260", "090430", "035250",
    "024110", "271560",
]

# ── 데이터 수집 ────────────────────────────────────────────────

def today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def days_ago_str(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y%m%d")


def get_universe(market: str = "KOSPI") -> list[str]:
    """KRX 시장의 전체 종목코드 반환"""
    if not PYKRX_OK:
        return KOSPI_SAMPLE
    try:
        tickers = krx.get_market_ticker_list(today_str(), market=market)
        return [t for t in tickers if t]
    except Exception:
        return KOSPI_SAMPLE


def get_ticker_name(ticker: str) -> str:
    if not PYKRX_OK:
        return ticker
    try:
        return krx.get_market_ticker_name(ticker)
    except Exception:
        return ticker


def get_price_series(ticker: str, start: str, end: str) -> pd.Series:
    """단일 종목 종가 시계열"""
    if not PYKRX_OK:
        return pd.Series(dtype=float)
    try:
        df = krx.get_market_ohlcv(start, end, ticker)
        if df.empty or "종가" not in df.columns:
            return pd.Series(dtype=float)
        s = df["종가"]
        s.index = pd.to_datetime(s.index)
        return s.dropna()
    except Exception:
        return pd.Series(dtype=float)


def get_prices_batch(
    tickers: list[str],
    start: str,
    end: str,
    verbose: bool = False,
) -> pd.DataFrame:
    """여러 종목 종가 DataFrame (날짜 × 종목)"""
    prices = {}
    for i, ticker in enumerate(tickers):
        if verbose and i % 10 == 0:
            print(f"  데이터 수집 중... {i}/{len(tickers)}", end="\r")
        s = get_price_series(ticker, start, end)
        if not s.empty:
            prices[ticker] = s
    if verbose:
        print()
    if not prices:
        return pd.DataFrame()
    df = pd.DataFrame(prices)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def get_fundamentals_batch(tickers: list[str], date: str) -> pd.DataFrame:
    """특정 날짜 PBR, PER, EPS (pykrx)"""
    if not PYKRX_OK:
        return pd.DataFrame()
    rows = []
    for ticker in tickers:
        try:
            df = krx.get_market_fundamental(date, date, ticker)
            if not df.empty:
                row = df.iloc[-1].to_dict()
                row["ticker"] = ticker
                rows.append(row)
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).set_index("ticker")
    result.index = result.index.astype(str)
    return result


# ── 팩터 계산 ─────────────────────────────────────────────────

def _momentum(prices: pd.DataFrame, window: int) -> pd.Series:
    """N일 수익률 모멘텀 (최신 1거래일 제외)"""
    if len(prices) < window + 2:
        return pd.Series(dtype=float)
    return (prices.iloc[-2] / prices.iloc[-(window + 1)] - 1).dropna()


def _rsi(prices: pd.DataFrame, window: int = 14) -> pd.Series:
    """RSI(14) — 값이 낮을수록 과매도"""
    results = {}
    for col in prices.columns:
        s = prices[col].dropna()
        if len(s) < window + 1:
            continue
        delta = s.diff()
        gain  = delta.clip(lower=0).ewm(span=window, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(span=window, adjust=False).mean()
        rs    = gain / loss.replace(0, np.nan)
        results[col] = (100 - 100 / (1 + rs)).iloc[-1]
    return pd.Series(results).dropna()


def _ma_cross(prices: pd.DataFrame, short: int = 20, long: int = 60) -> pd.Series:
    """단기/장기 MA 비율 (>0이면 골든크로스 영역)"""
    if len(prices) < long:
        return pd.Series(dtype=float)
    ma_s = prices.tail(short).mean()
    ma_l = prices.tail(long).mean()
    return (ma_s / ma_l.replace(0, np.nan) - 1).dropna()


def _volatility(prices: pd.DataFrame, window: int = 20) -> pd.Series:
    """N일 변동성 (낮을수록 안정적)"""
    if len(prices) < window:
        return pd.Series(dtype=float)
    return prices.pct_change().tail(window).std().dropna()


def _rank(s: pd.Series, ascending: bool = True) -> pd.Series:
    """백분위 랭크 0~1 정규화"""
    if s.empty:
        return s
    return s.rank(pct=True, ascending=ascending)


# ── 복합 스코어 계산 ──────────────────────────────────────────

DEFAULT_WEIGHTS = {
    "mom1m":     0.10,
    "mom3m":     0.20,
    "mom6m":     0.25,
    "ma_cross":  0.25,
    "rsi_rev":   0.20,   # RSI 낮을수록 높은 점수 (과매도 역추세)
}


def compute_scores(
    prices: pd.DataFrame,
    fundamentals: pd.DataFrame = None,
    weights: dict = None,
) -> pd.DataFrame:
    """
    복합 팩터 스코어 계산.
    Returns DataFrame: 종목명, 각 팩터 점수, composite(0~1)
    """
    if prices.empty:
        return pd.DataFrame()

    w = dict(weights or DEFAULT_WEIGHTS)
    factor_map = {}

    # 모멘텀
    m1 = _momentum(prices, 21)
    m3 = _momentum(prices, 63)
    m6 = _momentum(prices, 126)
    if not m1.empty:  factor_map["mom1m"]    = _rank(m1)
    if not m3.empty:  factor_map["mom3m"]    = _rank(m3)
    if not m6.empty:  factor_map["mom6m"]    = _rank(m6)

    # MA 크로스
    mac = _ma_cross(prices)
    if not mac.empty: factor_map["ma_cross"] = _rank(mac)

    # RSI 역방향
    rsi = _rsi(prices)
    if not rsi.empty: factor_map["rsi_rev"]  = _rank(-rsi)

    # PBR 역방향 (저평가 선호)
    if fundamentals is not None and not fundamentals.empty and "PBR" in fundamentals.columns:
        pbr = fundamentals["PBR"].dropna()
        pbr = pbr[pbr > 0]
        if not pbr.empty:
            factor_map["pbr_rev"] = _rank(-pbr)
            w["pbr_rev"] = 0.15
            # 가중치 재정규화
            total = sum(w.values())
            w = {k: v / total for k, v in w.items()}

    if not factor_map:
        return pd.DataFrame()

    df = pd.DataFrame(factor_map)
    # 절반 이상 팩터가 있는 종목만
    df = df.dropna(thresh=max(1, len(factor_map) // 2))

    # 가중 composite
    composite = pd.Series(0.0, index=df.index)
    for fname, weight in w.items():
        if fname in df.columns:
            composite += df[fname].fillna(0.5) * weight
    df["composite"] = composite

    # 종목명 추가
    names = {t: get_ticker_name(t) for t in df.index}
    df.insert(0, "종목명", pd.Series(names))

    return df.sort_values("composite", ascending=False)


# ── 신호 생성 ─────────────────────────────────────────────────

def generate_signals(
    scores: pd.DataFrame,
    top_n: int = 20,
    buy_threshold: float = 0.65,
    sell_threshold: float = 0.35,
) -> pd.DataFrame:
    """
    composite 점수로 매수/관망/매도 신호 생성.
    Returns DataFrame: 종목명, composite, rank, signal
    """
    if scores.empty:
        return pd.DataFrame()

    sig = scores[["종목명", "composite"]].copy()
    sig["rank"]   = range(1, len(sig) + 1)
    sig["signal"] = "NEUTRAL"
    sig.loc[sig["composite"] >= buy_threshold, "signal"] = "BUY"
    sig.loc[sig["composite"] <= sell_threshold, "signal"] = "SELL"

    return sig.head(top_n * 2)


# ── 백테스팅 ─────────────────────────────────────────────────

def run_backtest(
    tickers: list[str],
    start_date: str,          # YYYYMMDD
    end_date: str,            # YYYYMMDD
    top_n: int = 20,
    rebalance_months: int = 1,
    initial_capital: float = 100_000_000,
    verbose: bool = True,
) -> dict:
    """
    월별 리밸런싱 백테스트.
    매 리밸런싱 시점에 composite 상위 top_n 종목을 동일 비중으로 보유.
    """
    if verbose:
        print(f"  가격 데이터 수집 중 ({len(tickers)}개 종목)...")

    prices = get_prices_batch(tickers, start_date, end_date, verbose=verbose)
    if prices.empty:
        return {"error": "가격 데이터 없음"}

    # 리밸런싱 날짜 생성 (월말 기준)
    date_range = pd.date_range(
        start=pd.to_datetime(start_date, format="%Y%m%d"),
        end=pd.to_datetime(end_date,   format="%Y%m%d"),
        freq=f"{rebalance_months}ME",
    )
    rebal_dates = [d for d in date_range if d in prices.index or
                   prices.index[prices.index <= d].size > 0]
    # 실제 존재하는 거래일로 스냅
    rebal_dates = [
        prices.index[prices.index <= d][-1]
        for d in date_range
        if prices.index[prices.index <= d].size > 0
    ]
    rebal_dates = sorted(set(rebal_dates))

    if len(rebal_dates) < 2:
        return {"error": "리밸런싱 날짜 부족 (기간을 늘려주세요)"}

    portfolio_log = []
    holdings_log  = []
    capital       = initial_capital

    for i in range(len(rebal_dates) - 1):
        d_now  = rebal_dates[i]
        d_next = rebal_dates[i + 1]

        hist = prices.loc[:d_now]
        if len(hist) < 70:
            continue

        scores = compute_scores(hist)
        if scores.empty:
            continue

        top = scores.head(top_n).index.tolist()
        holdings_log.append({"date": d_now, "tickers": top})

        # 보유 기간 수익률
        period = prices.loc[d_now:d_next, [t for t in top if t in prices.columns]]
        period = period.dropna(axis=1, how="all")
        if period.empty or len(period) < 2:
            portfolio_log.append({"date": d_next, "value": capital})
            continue

        rets = (period.iloc[-1] / period.iloc[0] - 1).dropna()
        period_ret = rets.mean()
        capital   *= 1 + period_ret
        portfolio_log.append({"date": d_next, "value": capital})

    if not portfolio_log:
        return {"error": "수익률 계산 실패"}

    pv = pd.DataFrame(portfolio_log).set_index("date")["value"]
    total_ret = (capital - initial_capital) / initial_capital
    n_days    = (rebal_dates[-1] - rebal_dates[0]).days
    n_years   = n_days / 365.25
    cagr      = (1 + total_ret) ** (1 / max(n_years, 0.1)) - 1

    cummax    = pv.cummax()
    mdd       = ((pv - cummax) / cummax).min()

    monthly   = pv.pct_change().dropna()
    sharpe    = (
        monthly.mean() / monthly.std() * (12 ** 0.5)
        if monthly.std() > 0 else 0.0
    )
    win_rate  = (monthly > 0).mean()

    return {
        "initial_capital": initial_capital,
        "final_value":     capital,
        "total_return":    total_ret,
        "cagr":            cagr,
        "mdd":             mdd,
        "sharpe":          sharpe,
        "win_rate":        win_rate,
        "n_rebalances":    len(portfolio_log),
        "portfolio_values": pv,
        "holdings_log":    holdings_log,
    }


def format_backtest_result(result: dict) -> str:
    """백테스트 결과를 텍스트로 포맷"""
    if "error" in result:
        return f"백테스트 실패: {result['error']}"

    lines = [
        "📊 백테스트 결과",
        f"  초기 자본:    {result['initial_capital']:>15,.0f}원",
        f"  최종 자산:    {result['final_value']:>15,.0f}원",
        f"  총 수익률:    {result['total_return']:>+.1%}",
        f"  CAGR:         {result['cagr']:>+.1%}",
        f"  MDD:          {result['mdd']:.1%}",
        f"  Sharpe:       {result['sharpe']:.2f}",
        f"  월간 승률:    {result['win_rate']:.1%}",
        f"  리밸런싱 횟수: {result['n_rebalances']}회",
    ]
    return "\n".join(lines)
