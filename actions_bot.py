"""
GitHub Actions 경량 봇 핸들러
10분마다 실행 → /quant 명령어 감지 → 퀀트 신호 전송
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta
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

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WINDOW_SEC = 12 * 60  # 12분 이내 메시지만 처리 (10분 주기 + 여유 2분)


# ── Telegram API 헬퍼 ─────────────────────────────────────────

def tg_get(method: str, **params) -> dict:
    res = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        params=params, timeout=15,
    )
    return res.json()


def tg_send(chat_id, text: str) -> None:
    max_len = 4000
    for chunk in [text[i:i+max_len] for i in range(0, len(text), max_len)]:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
            timeout=15,
        )
        time.sleep(0.5)


# ── 최근 메시지에서 명령어 추출 ───────────────────────────────

def get_recent_commands() -> list[dict]:
    """
    최근 100개 업데이트에서 WINDOW_SEC 이내의 명령어만 반환.
    허가된 CHAT_ID 에서 온 것만 처리.
    """
    data = tg_get("getUpdates", limit=100, timeout=0)
    updates = data.get("result", [])

    print(f"  [DEBUG] getUpdates 응답: ok={data.get('ok')}, 업데이트 수={len(updates)}")
    print(f"  [DEBUG] 설정된 CHAT_ID: '{CHAT_ID}'")

    now    = datetime.now(timezone.utc).timestamp()
    cmds   = []

    for upd in updates:
        msg  = upd.get("message") or upd.get("edited_message", {})
        if not msg:
            continue
        text    = msg.get("text", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        ts      = msg.get("date", 0)
        age_sec = now - ts

        # 전체 메시지 현황 출력 (최근 30분 이내)
        if age_sec < 1800:
            print(f"  [DEBUG] 메시지: chat_id={chat_id}, age={age_sec:.0f}s, text='{text[:30]}'")

        # 허가된 채팅 + 시간 윈도우 필터
        if chat_id != str(CHAT_ID):
            if text.startswith("/"):
                print(f"  [DEBUG] CHAT_ID 불일치 → 수신={chat_id}, 기대={CHAT_ID}")
            continue
        if now - ts > WINDOW_SEC:
            continue
        if not text.startswith("/"):
            continue

        cmds.append({
            "chat_id":   chat_id,
            "text":      text.strip(),
            "update_id": upd.get("update_id"),
            "ts":        ts,
        })

    return cmds


# ── 명령어 처리 ───────────────────────────────────────────────

def handle_quant(chat_id: str, args: list[str]) -> None:
    top_n = 10
    for a in args:
        if a.isdigit():
            top_n = int(a)

    tg_send(chat_id, f"⏳ 퀀트 신호 분석 중... (약 2~3분 소요)\n상위 {top_n}개 종목 팩터 스코어링 + AI 추천")

    try:
        from quant_runner import run_quant_signal
        # run_quant_signal 은 내부적으로 send_telegram 을 호출하므로
        # chat_id 가 TELEGRAM_CHAT_ID 와 동일하면 그냥 실행
        run_quant_signal(top_n=top_n)
    except Exception as e:
        tg_send(chat_id, f"❌ 퀀트 분석 실패: {e}")


def handle_market(chat_id: str) -> None:
    tg_send(chat_id, "⏳ 현재 시황 분석 중...")
    try:
        import yfinance as yf
        import anthropic

        tickers = {
            "S&P500": "^GSPC", "나스닥": "^IXIC", "VIX": "^VIX",
            "미10년물": "^TNX", "달러인덱스": "DX-Y.NYB",
            "WTI유가": "CL=F", "달러/원": "USDKRW=X", "한국ETF(EWY)": "EWY",
        }
        lines = []
        for name, sym in tickers.items():
            try:
                info  = yf.Ticker(sym).fast_info
                price = info.last_price
                prev  = info.previous_close
                if price and prev:
                    pct = (price - prev) / prev * 100
                    arrow = "▲" if pct >= 0 else "▼"
                    lines.append(f"{name:<14}: {price:>10,.2f}  {arrow}{pct:+.2f}%")
            except Exception:
                pass

        macro_text = "\n".join(lines)
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        msg = f"📊 *현재 시황* `{now}`\n```\n{macro_text}\n```"
        tg_send(chat_id, msg)
    except Exception as e:
        tg_send(chat_id, f"❌ 시황 조회 실패: {e}")


def handle_portfolio(chat_id: str) -> None:
    tg_send(chat_id, "⏳ 포트폴리오 현황 조회 중...")
    try:
        from quant_portfolio import get_portfolio_status, next_rebalance_date
        status = get_portfolio_status()
        next_r = next_rebalance_date()
        tg_send(chat_id, f"{status}\n\n📅 다음 리밸런싱 권장일: {next_r}")
    except Exception as e:
        tg_send(chat_id, f"❌ 포트폴리오 조회 실패: {e}")


HANDLERS = {
    "/quant":     lambda chat_id, args: handle_quant(chat_id, args),
    "/market":    lambda chat_id, args: handle_market(chat_id),
    "/portfolio": lambda chat_id, args: handle_portfolio(chat_id),
}


def main():
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
        sys.exit(1)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 명령어 확인 시작...")
    cmds = get_recent_commands()

    if not cmds:
        print("  → 처리할 명령어 없음")
        return

    # 중복 방지: 동일 명령어가 여러 개면 가장 최신 1개만
    seen   = set()
    unique = []
    for c in sorted(cmds, key=lambda x: x["ts"], reverse=True):
        base = c["text"].split()[0].lower()
        if base not in seen:
            seen.add(base)
            unique.append(c)

    for cmd in unique:
        parts   = cmd["text"].split()
        command = parts[0].lower().split("@")[0]  # /quant@botname → /quant
        args    = parts[1:]
        chat_id = cmd["chat_id"]

        print(f"  → 명령어 처리: {command} {args}")

        handler = HANDLERS.get(command)
        if handler:
            handler(chat_id, args)
        else:
            print(f"  → 미지원 명령어: {command}")

    print("  → 완료")


if __name__ == "__main__":
    main()
