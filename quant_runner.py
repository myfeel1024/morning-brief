"""
퀀트 신호 자동 전송 스크립트 (GitHub Actions 실행용)
텔레그램 봇 없이 직접 API로 전송
"""

import os
import sys
import requests
from datetime import datetime
from pathlib import Path


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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")


def send_telegram(text: str, chat_id: str = "") -> None:
    target = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not target:
        print(text)
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len = 4000
    chunks  = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    for chunk in chunks:
        try:
            res = requests.post(url, data={
                "chat_id": target,
                "text":    chunk,
            }, timeout=10)
            if res.status_code != 200:
                print(f"전송 오류: {res.text}")
        except Exception as e:
            print(f"전송 실패: {e}")


def run_quant_signal(top_n: int = 10, chat_id: str = "") -> None:
    """퀀트 신호 계산 후 텔레그램 전송"""
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    print(f"[{now}] 퀀트 신호 생성 시작...")

    try:
        from quant_engine import (
            get_prices_batch, compute_scores, generate_signals,
            days_ago_str, today_str, KOSPI_SAMPLE,
        )
        from quant_portfolio import get_market_filter, format_market_filter

        # 시장 필터
        print("  → 시장 필터 확인 중...")
        mf     = get_market_filter()
        mf_txt = format_market_filter(mf)

        # 가격 데이터 + 팩터 계산
        print("  → 가격 데이터 수집 중...")
        prices  = get_prices_batch(KOSPI_SAMPLE, days_ago_str(200), today_str(), verbose=False)
        if prices.empty:
            send_telegram("⚠️ 퀀트 신호: 가격 데이터 수집 실패", chat_id)
            return

        print("  → 팩터 스코어 계산 중...")
        scores  = compute_scores(prices)
        signals = generate_signals(scores, top_n=top_n)

        # 신호 테이블 구성
        lines = []
        for ticker, row in signals.head(top_n).iterrows():
            icon  = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}.get(row["signal"], "⚪")
            mom3m = scores.loc[ticker, "mom3m"] if "mom3m" in scores.columns and ticker in scores.index else None
            mom_s = f"{mom3m*100:+.0f}%" if mom3m is not None and mom3m == mom3m else "N/A"
            lines.append(
                f"{int(row['rank']):2}. {str(row['종목명']):<10} "
                f"{row['composite']:.2f} {icon}{row['signal']:<7} {mom_s}"
            )
        table = "\n".join(lines)

        # AI 추천
        ai_text = ""
        if ANTHROPIC_API_KEY:
            print("  → AI 추천 생성 중...")
            try:
                from quant_ai import get_ai_recommendation
                top_list = [
                    {"ticker": t, "name": row["종목명"],
                     "score": row["composite"], "signal": row["signal"]}
                    for t, row in signals.head(top_n).iterrows()
                ]
                ai_text = "\n\n🤖 AI 퀀트 전략\n" + get_ai_recommendation(top_list)
            except Exception as e:
                ai_text = f"\n\nAI 분석 실패: {e}"

        # 메시지 조합 (parse_mode 없이 일반 텍스트)
        separator = "\n" + "─" * 30 + "\n"
        message = (
            f"📊 퀀트 신호 브리핑 {now}"
            + separator
            + mf_txt
            + separator
            + f"KOSPI 팩터 신호 상위 {top_n}\n"
            + f"순위  종목명         점수  신호      3M수익\n"
            + "─" * 42 + "\n"
            + table
            + ai_text
            + separator
            + "📌 팩터: 모멘텀(1M/3M/6M) + MA크로스 + RSI 역추세\n"
            + "본 신호는 투자 참고용이며 투자 결정의 책임은 본인에게 있습니다."
        )

        print("  → 텔레그램 전송 중...")
        send_telegram(message, chat_id)
        print(f"[{now}] 완료!")

    except Exception as e:
        err_msg = f"⚠️ 퀀트 신호 생성 실패: {e}"
        print(err_msg)
        send_telegram(err_msg, chat_id)
        sys.exit(1)


if __name__ == "__main__":
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    run_quant_signal(top_n=top_n)
