"""
미국 퀀트 엔진 — S&P 500 상위 + Nasdaq 100
팩터: 모멘텀(1/3/6m) + MA크로스 + RSI 역발
"""

import os
import numpy as np
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, timezone

ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_IDS         = [s.strip() for s in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if s.strip()]

# ── 유니버스: S&P 500 상위 100 + Nasdaq 100 주요 종목 ────────────
US_UNIVERSE = [
    # S&P 500 메가캡 / 상위 시총
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","BRK-B",
    "TSLA","AVGO","JPM","LLY","V","UNH","XOM","MA","COST",
    "HD","PG","NFLX","JNJ","ABBV","BAC","CRM","WMT","AMD",
    "MRK","ORCL","CVX","ACN","TMO","LIN","MCD","QCOM","GE",
    "ADBE","IBM","TXN","PM","INTU","CAT","ISRG","GS","AMGN",
    "SPGI","BKNG","HON","NOW","RTX","UNP","AXP","VRTX","MS",
    "T","LOW","PANW","SYK","AMAT","BLK","ADI","TMUS","DE",
    "PLD","REGN","GILD","MMC","ETN","ADP","BSX","MDLZ","MU",
    "PGR","CB","ZTS","SCHW","FI","CMG","INTC","AMT","CI",
    "KLAC","SO","CEG","WM","EOG","NOC","HCA","APH","DUK",
    "CL","CDNS","TJX","SNPS","MCO","GD","CSX","USB",
    # Nasdaq 100 추가 (S&P 상위 미포함)
    "PLTR","ARM","SMCI","CRWD","MRVL","FTNT","DDOG","ZS",
    "TEAM","NET","SNOW","ABNB","UBER","COIN","APP","MSTR",
    "LRCX","ASML","MDB","WDAY","ANSS","IDXX","FANG","VRSK",
    "CTAS","FAST","ROST","PAYX","CPRT","ODFL","PCAR","KDP",
]

WEIGHTS = {
    "mom1m": 0.10,
    "mom3m": 0.20,
    "mom6m": 0.25,
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
            price = float(s.iloc[-1])
            mom1m = float(s.iloc[-1] / s.iloc[-21] - 1) if len(s) >= 21 else np.nan
            mom3m = float(s.iloc[-1] / s.iloc[-63] - 1) if len(s) >= 63 else np.nan
            mom6m = float(s.iloc[-1] / s.iloc[-126] - 1) if len(s) >= 126 else np.nan

            ma20 = float(s.rolling(20).mean().iloc[-1])
            ma60 = float(s.rolling(60).mean().iloc[-1])
            ma_cross = 1.0 if ma20 > ma60 else 0.0

            delta = s.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rsi   = float((100 - 100 / (1 + gain / loss.replace(0, np.nan))).iloc[-1])
            rsi_rev = 1 - rsi / 100

            records.append({
                "ticker": ticker, "price": price,
                "mom1m": mom1m, "mom3m": mom3m, "mom6m": mom6m,
                "ma_cross": ma_cross, "rsi": rsi, "rsi_rev": rsi_rev,
            })
        except Exception:
            continue

    df = pd.DataFrame(records).set_index("ticker")
    for col in ["mom1m", "mom3m", "mom6m", "rsi_rev"]:
        df[f"{col}_r"] = df[col].rank(pct=True, na_option="bottom")

    df["score"] = (
        df["mom1m_r"]   * WEIGHTS["mom1m"] +
        df["mom3m_r"]   * WEIGHTS["mom3m"] +
        df["mom6m_r"]   * WEIGHTS["mom6m"] +
        df["ma_cross"]  * WEIGHTS["ma_cross"] +
        df["rsi_rev_r"] * WEIGHTS["rsi_rev"]
    )
    df["signal"] = df["score"].apply(
        lambda x: "BUY" if x >= 0.65 else ("SELL" if x <= 0.35 else "NEUTRAL")
    )
    return df.sort_values("score", ascending=False)


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


def run_us_quant(top_n: int = 15) -> str:
    print("[US Quant] 데이터 다운로드 중...", flush=True)
    raw = yf.download(
        US_UNIVERSE,
        period="1y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
    )

    # MultiIndex → Close 컬럼만 추출
    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"]
    else:
        closes = raw[["Close"]].rename(columns={"Close": US_UNIVERSE[0]})

    print(f"[US Quant] 팩터 계산 중... ({closes.shape[1]}개 종목)", flush=True)
    df = _compute_factors(closes)

    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top  = df.head(top_n)
    sell = df[df["signal"] == "SELL"].head(5)

    lines = [
        f"🇺🇸 미국 퀀트 신호 ({now})",
        f"유니버스: S&P500 상위 + Nasdaq100 ({len(df)}개 분석)",
        "",
        f"📈 상위 {top_n}종목",
    ]
    for i, (ticker, row) in enumerate(top.iterrows(), 1):
        sig  = "🟢" if row["signal"] == "BUY" else "🟡"
        m3   = f"{row['mom3m']*100:+.1f}%" if not np.isnan(row["mom3m"]) else "N/A"
        m6   = f"{row['mom6m']*100:+.1f}%" if not np.isnan(row["mom6m"]) else "N/A"
        ma   = "↑" if row["ma_cross"] else "↓"
        lines.append(
            f"{i:2}. {sig} {ticker:<6}  스코어:{row['score']:.2f}  "
            f"3M:{m3}  6M:{m6}  RSI:{row['rsi']:.0f}  MA:{ma}  ${row['price']:,.1f}"
        )

    if not sell.empty:
        lines += ["", "📉 매도 주의"]
        for ticker, row in sell.iterrows():
            lines.append(
                f"🔴 {ticker:<6}  스코어:{row['score']:.2f}  "
                f"RSI:{row['rsi']:.0f}  ${row['price']:,.1f}"
            )

    # AI 코멘트
    try:
        import anthropic
        top5 = ", ".join(top.head(5).index.tolist())
        ai_text = anthropic.Anthropic(api_key=ANTHROPIC_KEY).messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            messages=[{"role": "user", "content": (
                f"미국 퀀트 모델 상위 5종목: {top5}\n"
                "팩터: 6개월 모멘텀(25%) + MA크로스(25%) + 3개월 모멘텀(20%) + RSI역발(20%) + 1개월 모멘텀(10%)\n\n"
                "이 종목들의 공통 섹터/테마, 현재 시장 맥락에서 주목할 점을 3줄 이내 한국어로 설명. "
                "마크다운 헤더·볼드 금지, 이모지 사용 가능."
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
