"""
KRX 퀀트 AI 투자 툴 — CLI & AI 종목 추천

사용법:
  python quant_ai.py                    # 상위 20개 종목 신호 출력
  python quant_ai.py --top 10           # 상위 10개
  python quant_ai.py --backtest         # 백테스트 실행
  python quant_ai.py --market KOSDAQ    # 코스닥 분석
  python quant_ai.py --ticker 005930    # 특정 종목 상세 분석
"""

import os
import sys
import argparse
from datetime import datetime
from pathlib import Path

# .env 로드
def _load_env():
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    with open(env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()

import anthropic
from quant_engine import (
    get_universe, get_prices_batch, get_fundamentals_batch,
    compute_scores, generate_signals, run_backtest,
    format_backtest_result, days_ago_str, today_str,
    get_ticker_name, KOSPI_SAMPLE,
)

try:
    from stock_research import get_google_news, fetch_korean_news
    RESEARCH_OK = True
except ImportError:
    RESEARCH_OK = False

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")


# ── 헬퍼 ─────────────────────────────────────────────────────

def _claude():
    if not ANTHROPIC_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def _signal_icon(signal: str) -> str:
    return {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}.get(signal, "⚪")


# ── 신호 출력 ─────────────────────────────────────────────────

def print_signals(signals, scores):
    """신호 테이블 콘솔 출력"""
    print(f"\n{'─'*60}")
    print(f"{'순위':>4}  {'종목명':<12} {'점수':>6}  {'신호':<8}  {'모멘텀(3M)':>10}")
    print(f"{'─'*60}")
    for ticker, row in signals.iterrows():
        score  = row["composite"]
        signal = row["signal"]
        name   = row["종목명"]
        icon   = _signal_icon(signal)
        mom3m  = scores.loc[ticker, "mom3m"] if "mom3m" in scores.columns and ticker in scores.index else float("nan")
        mom_str = f"{mom3m*100:+.0f}%" if not (mom3m != mom3m) else "  N/A"
        print(f"{row['rank']:>4}  {str(name):<12} {score:.3f}  {icon}{signal:<7}  {mom_str:>10}")
    print(f"{'─'*60}\n")


# ── AI 종목 추천 ──────────────────────────────────────────────

def get_ai_recommendation(top_stocks: list[dict], market_context: str = "") -> str:
    """
    상위 종목 리스트를 Claude에 전달해 투자 전략 생성.
    top_stocks: [{"ticker": ..., "name": ..., "score": ..., "signal": ...}, ...]
    """
    if not ANTHROPIC_KEY:
        return "⚠️ ANTHROPIC_API_KEY 미설정"

    stock_lines = "\n".join(
        f"  {i+1}. {s['name']}({s['ticker']}) — 점수 {s['score']:.3f}, 신호 {s['signal']}"
        for i, s in enumerate(top_stocks)
    )

    # 뉴스 수집 (있을 때만)
    news_section = ""
    if RESEARCH_OK:
        try:
            kr_news = fetch_korean_news(n=5)
            if kr_news:
                news_section = "\n[최근 국내 경제 뉴스]\n" + "\n".join(f"• {n}" for n in kr_news)
        except Exception:
            pass

    prompt = f"""당신은 한국 주식 퀀트 전략 전문가입니다.
아래는 팩터 모델(모멘텀 + 이동평균 크로스 + RSI 역추세)로 선별된 상위 종목 목록입니다.

[퀀트 신호 상위 종목]
{stock_lines}
{market_context}
{news_section}

다음 항목을 분석하여 한국어로 답변하세요:

① 전체 포트폴리오 관점 요약
   - 현재 신호가 강한 섹터/테마는?
   - 매크로 환경과의 정합성

② 상위 3~5개 종목 핵심 포인트
   - 왜 이 종목이 퀀트 신호 상위인지
   - 주의할 리스크

③ 포지션 전략 제안
   - BUY 신호 종목 중 우선순위
   - 진입 시 분산 방법 (섹터/비중)

④ 현재 시장 환경에서의 전략 조언

텔레그램 전송용 포맷 (500자 내외, 마크다운 헤더 없이 이모지 + 텍스트):"""

    try:
        msg = _claude().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        return f"AI 분석 실패: {e}"


# ── 단일 종목 상세 분석 ───────────────────────────────────────

def analyze_single_ticker(ticker: str) -> str:
    """특정 종목 팩터 상세 분석"""
    print(f"\n{ticker} 분석 중...")
    name   = get_ticker_name(ticker)
    start  = days_ago_str(300)
    end    = today_str()

    from quant_engine import get_price_series, _momentum, _rsi, _ma_cross, _volatility
    s = get_price_series(ticker, start, end)
    if s.empty:
        return f"{ticker}: 가격 데이터 없음"

    prices_df = s.to_frame(name=ticker)

    result = [f"\n📈 [{name}({ticker})] 팩터 분석\n{'─'*40}"]

    # 현재가 & 수익률
    cur   = s.iloc[-1]
    prev  = s.iloc[-2] if len(s) > 1 else cur
    ret1d = (cur / prev - 1) * 100
    result.append(f"현재가:   {cur:>10,.0f}원  ({ret1d:+.2f}%)")

    # 모멘텀
    for label, w in [("1개월", 21), ("3개월", 63), ("6개월", 126)]:
        m = _momentum(prices_df, w)
        if not m.empty:
            v = m.iloc[0] * 100
            result.append(f"모멘텀 {label}: {v:>+.1f}%")

    # RSI
    rsi = _rsi(prices_df)
    if not rsi.empty:
        v = rsi.iloc[0]
        state = "과매수⚠️" if v > 70 else "과매도🟢" if v < 30 else "중립"
        result.append(f"RSI(14):  {v:>6.1f}  ({state})")

    # MA 크로스
    ma = _ma_cross(prices_df, 20, 60)
    if not ma.empty:
        v = ma.iloc[0] * 100
        cross = "골든크로스🟢" if v > 0 else "데드크로스🔴"
        result.append(f"MA크로스: {v:>+.2f}%  ({cross})")

    # 변동성
    vol = _volatility(prices_df, 20)
    if not vol.empty:
        result.append(f"변동성(20일): {vol.iloc[0]*100:.2f}%")

    # 52주 고저
    if len(s) >= 252:
        hi52 = s.tail(252).max()
        lo52 = s.tail(252).min()
        pos  = (cur - lo52) / (hi52 - lo52) * 100
        result.append(f"52주 위치:  {pos:.0f}%  (고가 {hi52:,.0f} / 저가 {lo52:,.0f})")

    # AI 코멘트
    if ANTHROPIC_KEY:
        news = []
        if RESEARCH_OK:
            try:
                news = get_google_news(f"{name} 주식 실적 전망", 4)
            except Exception:
                pass
        news_txt = "\n".join(f"• {n}" for n in news) if news else "뉴스 없음"
        prompt = (
            f"한국 주식 {name}({ticker}) 의 팩터 분석 결과:\n"
            + "\n".join(result[1:])
            + f"\n\n최신 뉴스:\n{news_txt}\n\n"
            "이 데이터를 바탕으로 투자자 관점에서 핵심 포인트 3가지를 간결하게 정리하세요."
            " (200자 이내, 한국어)"
        )
        try:
            ai = _claude().messages.create(
                model="claude-sonnet-4-6", max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            ).content[0].text
            result.append(f"\n🤖 AI 분석\n{ai}")
        except Exception:
            pass

    return "\n".join(result)


# ── 메인 CLI ─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="KRX 퀀트 AI 투자 툴")
    parser.add_argument("--market",   default="KOSPI",    help="KOSPI / KOSDAQ")
    parser.add_argument("--top",      type=int, default=20, help="상위 N개 종목")
    parser.add_argument("--backtest", action="store_true",  help="백테스트 실행")
    parser.add_argument("--bt-start", default=None,         help="백테스트 시작일 YYYYMMDD")
    parser.add_argument("--bt-end",   default=None,         help="백테스트 종료일 YYYYMMDD")
    parser.add_argument("--ticker",   default=None,         help="특정 종목 상세 분석")
    parser.add_argument("--no-ai",    action="store_true",  help="AI 분석 생략")
    parser.add_argument("--sample",   action="store_true",  help="대표 50종목만 사용 (빠름)")
    args = parser.parse_args()

    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    print(f"\n{'='*60}")
    print(f"  KRX 퀀트 AI 투자 툴  |  {now}")
    print(f"{'='*60}")

    # 시장 필터 항상 먼저 출력
    try:
        from quant_portfolio import get_market_filter, format_market_filter
        mf = get_market_filter()
        print(f"\n{format_market_filter(mf)}")
        if mf["status"] == "RISK_OFF" and not args.backtest and not args.ticker:
            print("⚠️  RISK_OFF: 코스피가 60MA 아래입니다. 신규 매수 자제 권고.\n")
    except Exception:
        pass

    # ── 단일 종목 분석 ──
    if args.ticker:
        print(analyze_single_ticker(args.ticker))
        return

    # ── 백테스트 ──
    if args.backtest:
        bt_start = args.bt_start or days_ago_str(730)   # 기본 2년
        bt_end   = args.bt_end   or today_str()
        print(f"\n백테스트 기간: {bt_start} ~ {bt_end}")
        tickers = KOSPI_SAMPLE if args.sample else get_universe(args.market)[:80]
        result  = run_backtest(
            tickers, bt_start, bt_end,
            top_n=args.top, verbose=True,
        )
        print(format_backtest_result(result))
        return

    # ── 전체 시장 신호 분석 ──
    print(f"\n{args.market} 시장 팩터 분석 시작...")
    tickers = KOSPI_SAMPLE if args.sample else get_universe(args.market)

    if len(tickers) > 100 and not args.sample:
        print(f"  종목 수: {len(tickers)}개 → 상위 100개로 제한 (빠른 실행을 원하면 --sample 옵션 사용)")
        tickers = tickers[:100]

    start = days_ago_str(200)
    end   = today_str()
    print(f"  기간: {start} ~ {end}  |  종목: {len(tickers)}개")
    print("  가격 데이터 수집 중...", flush=True)

    prices = get_prices_batch(tickers, start, end, verbose=True)
    if prices.empty:
        print("❌ 가격 데이터 수집 실패. pykrx 설치 여부를 확인하세요: pip install pykrx")
        return

    print("  팩터 스코어 계산 중...")
    scores  = compute_scores(prices)
    signals = generate_signals(scores, top_n=args.top)

    print_signals(signals, scores)

    # AI 추천
    if not args.no_ai and ANTHROPIC_KEY:
        print("🤖 AI 종목 추천 생성 중...\n")
        top_list = [
            {
                "ticker": ticker,
                "name":   row["종목명"],
                "score":  row["composite"],
                "signal": row["signal"],
            }
            for ticker, row in signals.head(args.top).iterrows()
        ]
        ai_text = get_ai_recommendation(top_list)
        print(f"{'─'*60}")
        print("🤖 AI 투자 전략")
        print(f"{'─'*60}")
        print(ai_text)
        print(f"{'─'*60}\n")
    elif not ANTHROPIC_KEY:
        print("ℹ️  AI 분석 생략 (.env에 ANTHROPIC_API_KEY 설정 시 활성화)")

    print("📌 본 분석은 투자 참고용이며, 투자 결정의 책임은 본인에게 있습니다.\n")


if __name__ == "__main__":
    main()
