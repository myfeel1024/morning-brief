"""
미국 퀀트 엔진 — GICS 11개 섹터 대표주 5종목씩 (총 55종목)
팩터: 모멘텀(1/3/6m) + MA크로스 + RSI 역발
"""

import os
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, timezone, timedelta

ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TOKEN         = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS      = [s.strip() for s in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if s.strip()]

# ── 섹터별 대표주 5종목 (GICS 11개 섹터) ──────────────────────
SECTOR_UNIVERSE = {
    "💻 기술":         ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL"],
    "📡 커뮤니케이션": ["META", "GOOGL", "NFLX", "T",    "VZ"  ],
    "🛒 경기소비재":   ["AMZN", "TSLA", "HD",   "MCD",  "NKE" ],
    "🧴 필수소비재":   ["WMT",  "PG",   "COST", "KO",   "PEP" ],
    "🏥 헬스케어":     ["LLY",  "UNH",  "JNJ",  "ABBV", "TMO" ],
    "🏦 금융":         ["JPM",  "BAC",  "GS",   "V",    "MA"  ],
    "🏭 산업재":       ["GE",   "CAT",  "HON",  "RTX",  "UNP" ],
    "⛽ 에너지":       ["XOM",  "CVX",  "EOG",  "COP",  "SLB" ],
    "⚗️ 소재":         ["LIN",  "APD",  "NEM",  "FCX",  "DOW" ],
    "🏢 리츠/부동산":  ["PLD",  "AMT",  "EQIX", "SPG",  "AVB" ],
    "⚡ 유틸리티":     ["NEE",  "DUK",  "SO",   "D",    "AEP" ],
}

# S&P 500 시총 상위 50 (섹터 대표주와 중복 허용 — 합산 후 중복 제거)
SP500_TOP50 = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","BRK-B","TSLA","AVGO",
    "JPM","LLY","V","UNH","XOM","MA","COST","HD","PG","NFLX",
    "JNJ","ABBV","BAC","CRM","WMT","AMD","MRK","ORCL","CVX","ACN",
    "TMO","LIN","MCD","QCOM","GE","ADBE","IBM","TXN","PM","INTU",
    "CAT","ISRG","GS","AMGN","SPGI","BKNG","HON","NOW","RTX","UNP",
]

_SECTOR_TICKERS = [t for tickers in SECTOR_UNIVERSE.values() for t in tickers]
ALL_TICKERS     = list(dict.fromkeys(_SECTOR_TICKERS + SP500_TOP50))  # 중복 제거, 순서 유지

WEIGHTS = {
    "mom1m":   0.10,
    "mom3m":   0.20,
    "mom6m":   0.25,
    "ma_cross": 0.25,
    "rsi_rev": 0.20,
}


def _compute_factors(closes: pd.DataFrame) -> pd.DataFrame:
    records = []
    for ticker in closes.columns:
        s = closes[ticker].dropna()
        if len(s) < 63:
            continue
        try:
            price  = float(s.iloc[-1])
            mom1m  = float(s.iloc[-1] / s.iloc[-21]  - 1) if len(s) >= 21  else np.nan
            mom3m  = float(s.iloc[-1] / s.iloc[-63]  - 1) if len(s) >= 63  else np.nan
            mom6m  = float(s.iloc[-1] / s.iloc[-126] - 1) if len(s) >= 126 else np.nan
            ma20   = float(s.rolling(20).mean().iloc[-1])
            ma60   = float(s.rolling(60).mean().iloc[-1])
            ma_cross = 1.0 if ma20 > ma60 else 0.0
            delta  = s.diff()
            gain   = delta.clip(lower=0).rolling(14).mean()
            loss   = (-delta.clip(upper=0)).rolling(14).mean()
            rsi    = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])
            records.append({
                "ticker": ticker, "price": price,
                "mom1m": mom1m, "mom3m": mom3m, "mom6m": mom6m,
                "ma_cross": ma_cross, "rsi": rsi, "rsi_rev": 1 - rsi / 100,
            })
        except Exception:
            continue

    df = pd.DataFrame(records).set_index("ticker")
    for col in ["mom1m", "mom3m", "mom6m", "rsi_rev"]:
        df[f"{col}_r"] = df[col].rank(pct=True, na_option="bottom")
    df["score"] = (
        df["mom1m_r"]   * WEIGHTS["mom1m"]  +
        df["mom3m_r"]   * WEIGHTS["mom3m"]  +
        df["mom6m_r"]   * WEIGHTS["mom6m"]  +
        df["ma_cross"]  * WEIGHTS["ma_cross"] +
        df["rsi_rev_r"] * WEIGHTS["rsi_rev"]
    )
    df["signal"] = df["score"].apply(
        lambda x: "🟢BUY" if x >= 0.65 else ("🔴SEL" if x <= 0.35 else "🟡HOL")
    )
    return df.sort_values("score", ascending=False)


def _fmt_row(ticker: str, row: pd.Series) -> str:
    m3  = f"{row['mom3m']*100:+.1f}%" if not np.isnan(row["mom3m"]) else " N/A "
    ma  = "↑" if row["ma_cross"] else "↓"
    return (
        f"{row['signal']} {ticker:<5} "
        f"스코어:{row['score']:.2f}  "
        f"3M:{m3}  RSI:{row['rsi']:.0f}  MA:{ma}  ${row['price']:,.1f}"
    )


def _send_telegram(text: str):
    if not TOKEN or not CHAT_IDS:
        return
    url    = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for cid in CHAT_IDS:
        for chunk in chunks:
            try:
                requests.post(url, json={"chat_id": cid, "text": chunk}, timeout=10)
            except Exception:
                pass


def run_us_quant(top_n: int = 10) -> str:
    print("[US Quant] 데이터 다운로드 중...", flush=True)
    raw = yf.download(
        ALL_TICKERS, period="1y", interval="1d",
        auto_adjust=True, progress=False, threads=True,
    )
    closes = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]

    print(f"[US Quant] 팩터 계산 중... ({closes.shape[1]}개 종목)", flush=True)
    df = _compute_factors(closes)

    # 티커 → 섹터 역매핑 (섹터 미분류는 S&P500 표기)
    ticker_to_sector = {t: sec for sec, tickers in SECTOR_UNIVERSE.items() for t in tickers}
    for t in SP500_TOP50:
        if t not in ticker_to_sector:
            ticker_to_sector[t] = "📊 S&P500"

    KST_TZ = timezone(timedelta(hours=9))
    kst  = datetime.now(KST_TZ).strftime("%Y년 %m월 %d일 %H:%M")

    lines = [
        f"🇺🇸 미국 퀀트 신호 브리핑 {kst}",
        f"섹터 대표주 55 + S&P500 상위50 ({len(df)}개 분석)",
        "",
        f"미국 팩터 신호 상위 {top_n}",
        f"{'순위':<4} {'종목':<6} {'섹터':<12} {'점수':>5}  {'신호':<8} {'3M수익':>7}  {'6M수익':>7}  RSI",
        "─" * 62,
    ]

    for i, (ticker, row) in enumerate(df.head(top_n).iterrows(), 1):
        sec  = ticker_to_sector.get(ticker, "S&P500")
        # 섹터명 이모지 제거해서 짧게
        sec_short = sec.split()[-1] if " " in sec else sec
        sig  = row["signal"]
        m3   = f"{row['mom3m']*100:+.1f}%" if not np.isnan(row["mom3m"]) else " N/A "
        m6   = f"{row['mom6m']*100:+.1f}%" if not np.isnan(row["mom6m"]) else " N/A "
        lines.append(
            f"{i:2}. {ticker:<6} [{sec_short:<10}]  "
            f"{row['score']:.2f}  {sig}  {m3:>7}  {m6:>7}  {row['rsi']:.0f}"
            f"  MA:{'↑' if row['ma_cross'] else '↓'}  ${row['price']:,.1f}"
        )

    # ── AI 코멘트 ──
    try:
        import anthropic
        top5 = ", ".join(df.head(5).index.tolist())
        # 섹터별 스코어 요약 (AI 입력용)
        sector_summary = []
        for sector, tickers in SECTOR_UNIVERSE.items():
            sub = df[df.index.isin(tickers)]
            if not sub.empty:
                avg = sub["score"].mean()
                sector_summary.append(f"{sector.split()[-1]}:{avg:.2f}")
        sector_str = ", ".join(sector_summary)

        ai_text = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": (
                f"미국 퀀트 전체 TOP5: {top5}\n"
                f"섹터별 평균 스코어: {sector_str}\n\n"
                "강세 섹터와 약세 섹터를 구분하고, 현재 시장에서 주목할 섹터 로테이션 흐름을 "
                "3줄 이내 한국어로 설명해주세요. 마크다운 헤더·볼드 금지, 이모지 사용 가능."
            )}],
        ).content[0].text
        lines += ["", f"🤖 AI 코멘트: {ai_text}"]
    except Exception:
        pass

    result = "\n".join(lines)
    _send_telegram(result)
    return result


if __name__ == "__main__":
    run_us_quant()
