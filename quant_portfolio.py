"""
퀀트 포트폴리오 매니저
- 시장 필터 (코스피 60MA 기준 RISK_ON/OFF)
- 월별 리밸런싱: 퀀트 상위 N종목 동일 비중
- 보유 종목 영속 저장 (portfolio.json)
- 수익률 추적
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import yfinance as yf

from quant_engine import (
    get_prices_batch, compute_scores, generate_signals,
    days_ago_str, today_str, get_ticker_name, KOSPI_SAMPLE,
)

PORTFOLIO_FILE = Path(__file__).resolve().parent / "portfolio.json"


# ── 시장 필터 ─────────────────────────────────────────────────

def get_market_filter(ma_window: int = 60) -> dict:
    """
    코스피 지수 vs 60일 이동평균.
    RISK_ON  → 코스피가 MA 위 (매수 가능)
    RISK_OFF → 코스피가 MA 아래 (현금 보유)
    """
    try:
        hist = yf.Ticker("^KS11").history(period="150d")
        if hist.empty or len(hist) < ma_window:
            return {"status": "UNKNOWN", "kospi": None, "ma60": None, "pct_diff": 0}

        kospi   = hist["Close"].iloc[-1]
        ma60    = hist["Close"].tail(ma_window).mean()
        pct     = (kospi - ma60) / ma60 * 100
        status  = "RISK_ON" if kospi > ma60 else "RISK_OFF"

        return {
            "status":   status,
            "kospi":    kospi,
            "ma60":     ma60,
            "pct_diff": pct,
        }
    except Exception as e:
        return {"status": "UNKNOWN", "kospi": None, "ma60": None, "pct_diff": 0, "error": str(e)}


def format_market_filter(mf: dict) -> str:
    status = mf["status"]
    icon   = {"RISK_ON": "🟢", "RISK_OFF": "🔴", "UNKNOWN": "⚪"}.get(status, "⚪")
    if mf["kospi"]:
        return (
            f"{icon} 시장 필터: {status}\n"
            f"  코스피 {mf['kospi']:,.0f}  |  60MA {mf['ma60']:,.0f}  |  "
            f"괴리 {mf['pct_diff']:+.2f}%"
        )
    return f"{icon} 시장 필터: {status} (데이터 없음)"


# ── 포트폴리오 저장/로드 ──────────────────────────────────────

def _load() -> dict:
    if PORTFOLIO_FILE.exists():
        try:
            with open(PORTFOLIO_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "holdings": {},          # {ticker: {name, entry_price, entry_date, weight}}
        "last_rebalance": None,
        "cash_mode": False,      # RISK_OFF로 인한 현금 보유 상태
    }


def _save(data: dict) -> None:
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_portfolio() -> dict:
    return _load()


# ── 현재 가격 조회 ────────────────────────────────────────────

def get_current_prices(tickers: list[str]) -> dict[str, float]:
    """보유 종목 현재가 일괄 조회"""
    prices = {}
    end   = today_str()
    start = days_ago_str(5)
    for ticker in tickers:
        try:
            from quant_engine import get_price_series
            s = get_price_series(ticker, start, end)
            if not s.empty:
                prices[ticker] = s.iloc[-1]
        except Exception:
            pass
    return prices


# ── 리밸런싱 계산 ─────────────────────────────────────────────

def compute_rebalance(
    top_n: int = 10,
    market: str = "KOSPI",
    use_sample: bool = True,
) -> dict:
    """
    퀀트 팩터 재계산 → 새 목표 포트폴리오 도출.
    Returns:
        {
          "market_filter": {...},
          "new_targets":   [ticker, ...],
          "to_buy":        [ticker, ...],
          "to_sell":       [ticker, ...],
          "to_hold":       [ticker, ...],
          "scores":        DataFrame,
          "cash_mode":     bool,
        }
    """
    portfolio = _load()

    # 1. 시장 필터 확인
    mf = get_market_filter()

    if mf["status"] == "RISK_OFF":
        return {
            "market_filter": mf,
            "new_targets":   [],
            "to_buy":        [],
            "to_sell":       list(portfolio["holdings"].keys()),
            "to_hold":       [],
            "scores":        None,
            "cash_mode":     True,
        }

    # 2. 팩터 스코어 계산
    tickers = KOSPI_SAMPLE if use_sample else []
    if not tickers:
        from quant_engine import get_universe
        tickers = get_universe(market)[:100]

    prices = get_prices_batch(tickers, days_ago_str(200), today_str())
    if prices.empty:
        return {"error": "가격 데이터 수집 실패", "market_filter": mf}

    scores  = compute_scores(prices)
    if scores.empty:
        return {"error": "팩터 계산 실패", "market_filter": mf}

    new_targets = scores.head(top_n).index.tolist()

    current  = set(portfolio["holdings"].keys())
    target   = set(new_targets)
    to_buy   = sorted(target - current)
    to_sell  = sorted(current - target)
    to_hold  = sorted(current & target)

    return {
        "market_filter": mf,
        "new_targets":   new_targets,
        "to_buy":        to_buy,
        "to_sell":       to_sell,
        "to_hold":       to_hold,
        "scores":        scores,
        "cash_mode":     False,
    }


def apply_rebalance(rebal: dict) -> None:
    """리밸런싱 결과를 portfolio.json에 저장"""
    if "error" in rebal:
        return

    portfolio = _load()

    if rebal["cash_mode"]:
        portfolio["holdings"]      = {}
        portfolio["cash_mode"]     = True
        portfolio["last_rebalance"] = datetime.now().strftime("%Y-%m-%d")
        _save(portfolio)
        return

    scores    = rebal["scores"]
    new_tickers = rebal["new_targets"]
    n         = len(new_tickers)
    weight    = round(1 / n, 4) if n > 0 else 0
    now_str   = datetime.now().strftime("%Y-%m-%d")

    # 현재가 조회
    cur_prices = get_current_prices(new_tickers)

    new_holdings = {}
    for ticker in new_tickers:
        name = scores.loc[ticker, "종목명"] if ticker in scores.index else get_ticker_name(ticker)
        score = scores.loc[ticker, "composite"] if ticker in scores.index else 0

        # 이미 보유 중이면 진입가 유지, 신규면 현재가로 기록
        if ticker in portfolio["holdings"]:
            entry_price = portfolio["holdings"][ticker].get("entry_price", 0)
            entry_date  = portfolio["holdings"][ticker].get("entry_date", now_str)
        else:
            entry_price = cur_prices.get(ticker, 0)
            entry_date  = now_str

        new_holdings[ticker] = {
            "name":        str(name),
            "entry_price": entry_price,
            "entry_date":  entry_date,
            "weight":      weight,
            "score":       round(float(score), 4),
        }

    portfolio["holdings"]       = new_holdings
    portfolio["cash_mode"]      = False
    portfolio["last_rebalance"] = now_str
    _save(portfolio)


# ── 포트폴리오 현황 조회 ──────────────────────────────────────

def get_portfolio_status() -> str:
    """보유 종목 현황 + 수익률 텍스트"""
    portfolio = _load()
    holdings  = portfolio.get("holdings", {})
    last_reb  = portfolio.get("last_rebalance", "없음")
    cash_mode = portfolio.get("cash_mode", False)

    if cash_mode:
        mf = get_market_filter()
        return (
            f"🔴 현재 현금 보유 모드 (RISK_OFF)\n"
            f"{format_market_filter(mf)}\n"
            f"마지막 리밸런싱: {last_reb}\n\n"
            f"코스피가 60MA 위로 회복되면 자동으로 RISK_ON으로 전환됩니다."
        )

    if not holdings:
        return "📂 보유 종목 없음. /rebalance 로 포트폴리오를 구성하세요."

    cur_prices = get_current_prices(list(holdings.keys()))

    lines   = []
    total_ret = 0.0
    valid_n = 0

    for ticker, info in holdings.items():
        name       = info.get("name", ticker)
        entry      = info.get("entry_price", 0)
        cur        = cur_prices.get(ticker, 0)
        entry_date = info.get("entry_date", "?")

        if entry > 0 and cur > 0:
            ret = (cur - entry) / entry * 100
            total_ret += ret
            valid_n   += 1
            ret_str = f"{ret:+.1f}%"
            icon    = "🔺" if ret >= 0 else "🔻"
        else:
            ret_str = "N/A"
            icon    = "⚪"

        lines.append(
            f"{icon} {str(name):<10} {cur:>8,.0f}원  {ret_str:>7}"
            f"  (진입 {entry:,.0f}원 · {entry_date})"
        )

    avg_ret = total_ret / valid_n if valid_n > 0 else 0

    mf = get_market_filter()
    header = (
        f"💼 포트폴리오 현황\n"
        f"마지막 리밸런싱: {last_reb}  |  종목 수: {len(holdings)}개\n"
        f"평균 수익률: {avg_ret:+.1f}%\n"
        f"{format_market_filter(mf)}\n"
        f"{'─'*42}\n"
    )
    return header + "\n".join(lines)


# ── 리밸런싱 결과 포맷 ────────────────────────────────────────

def format_rebalance(rebal: dict) -> str:
    if "error" in rebal:
        return f"❌ 오류: {rebal['error']}"

    mf_text = format_market_filter(rebal["market_filter"])
    lines   = [mf_text, ""]

    if rebal["cash_mode"]:
        lines += [
            "🔴 RISK_OFF — 전종목 매도 후 현금 보유 권고",
            "",
            "매도 대상:",
        ]
        for t in rebal["to_sell"]:
            lines.append(f"  🔴 매도: {get_ticker_name(t)} ({t})")
        return "\n".join(lines)

    scores = rebal["scores"]

    if rebal["to_buy"]:
        lines.append("🟢 신규 매수:")
        for t in rebal["to_buy"]:
            name  = scores.loc[t, "종목명"] if t in scores.index else get_ticker_name(t)
            score = scores.loc[t, "composite"] if t in scores.index else 0
            lines.append(f"  ✅ {str(name)} ({t})  점수 {score:.3f}")

    if rebal["to_sell"]:
        lines.append("\n🔴 매도 (목표 제외):")
        for t in rebal["to_sell"]:
            lines.append(f"  ❌ {get_ticker_name(t)} ({t})")

    if rebal["to_hold"]:
        lines.append("\n⚪ 유지:")
        for t in rebal["to_hold"]:
            name  = scores.loc[t, "종목명"] if t in scores.index else get_ticker_name(t)
            score = scores.loc[t, "composite"] if t in scores.index else 0
            lines.append(f"  🔄 {str(name)} ({t})  점수 {score:.3f}")

    n = len(rebal["new_targets"])
    if n > 0:
        lines += [
            "",
            f"📊 목표 비중: 각 {100/n:.1f}% (동일 비중, {n}종목)",
            "",
            "⚠️ 위 내용은 참고용입니다. 실제 매매는 직접 판단하여 진행하세요.",
        ]

    return "\n".join(lines)


# ── 다음 리밸런싱일 계산 ──────────────────────────────────────

def next_rebalance_date() -> str:
    """다음 달 마지막 금요일 반환"""
    today = datetime.now()
    # 다음 달 마지막 날
    if today.month == 12:
        last_day = datetime(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = datetime(today.year, today.month + 1, 1) - timedelta(days=1)
    # 마지막 금요일
    offset = (last_day.weekday() - 4) % 7  # 4 = 금요일
    last_friday = last_day - timedelta(days=offset)
    return last_friday.strftime("%Y-%m-%d")
