"""
GitHub Actions 경량 봇 핸들러
5분마다 실행 → 명령어/텍스트/사진 감지 → 처리 후 텔레그램 전송
"""

import base64
import os
import sys
import time
import requests
from datetime import datetime, timezone
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
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

WINDOW_SEC = 20 * 60  # 20분 이내 메시지만 처리


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
            data={"chat_id": chat_id, "text": chunk},
            timeout=15,
        )
        time.sleep(0.5)


# ── 최근 메시지 수집 (명령어 + 텍스트 + 사진) ────────────────

def get_recent_messages() -> list[dict]:
    data    = tg_get("getUpdates", limit=100, timeout=0)
    updates = data.get("result", [])

    print(f"  [DEBUG] getUpdates: ok={data.get('ok')}, 업데이트 수={len(updates)}")

    now  = datetime.now(timezone.utc).timestamp()
    msgs = []

    for upd in updates:
        msg     = upd.get("message") or upd.get("edited_message", {})
        if not msg:
            continue
        text    = msg.get("text", "") or msg.get("caption", "")
        chat_id = str(msg.get("chat", {}).get("id", ""))
        ts      = msg.get("date", 0)
        age_sec = now - ts

        if age_sec < 1800:
            preview = text[:30] if text else ("[사진]" if msg.get("photo") else "[기타]")
            print(f"  [DEBUG] chat_id={chat_id}, age={age_sec:.0f}s, msg='{preview}'")

        if CHAT_ID and chat_id != str(CHAT_ID):
            continue
        if age_sec > WINDOW_SEC:
            continue

        # 사진
        if msg.get("photo"):
            file_id = msg["photo"][-1].get("file_id", "")
            msgs.append({
                "type":    "photo",
                "chat_id": chat_id,
                "text":    text,
                "file_id": file_id,
                "update_id": upd.get("update_id"),
                "ts":      ts,
            })
        # 슬래시 명령어
        elif text.startswith("/"):
            msgs.append({
                "type":    "command",
                "chat_id": chat_id,
                "text":    text.strip(),
                "update_id": upd.get("update_id"),
                "ts":      ts,
            })
        # 일반 텍스트
        elif text.strip():
            msgs.append({
                "type":    "text",
                "chat_id": chat_id,
                "text":    text.strip(),
                "update_id": upd.get("update_id"),
                "ts":      ts,
            })

    return msgs


# ── 명령어 핸들러 ─────────────────────────────────────────────

def handle_quant(chat_id: str, args: list[str]) -> None:
    top_n = 10
    for a in args:
        if a.isdigit():
            top_n = int(a)
    tg_send(chat_id, f"⏳ 퀀트 신호 분석 중... (약 2~3분 소요)\n상위 {top_n}개 종목 팩터 스코어링 + AI 추천")
    try:
        from quant_runner import run_quant_signal
        run_quant_signal(top_n=top_n, chat_id=chat_id)
    except Exception as e:
        tg_send(chat_id, f"❌ 퀀트 분석 실패: {e}")


def handle_market(chat_id: str) -> None:
    tg_send(chat_id, "⏳ 현재 시황 분석 중...")
    try:
        import yfinance as yf
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
                    pct   = (price - prev) / prev * 100
                    arrow = "▲" if pct >= 0 else "▼"
                    lines.append(f"{name:<14}: {price:>10,.2f}  {arrow}{pct:+.2f}%")
            except Exception:
                pass
        now = datetime.now().strftime("%Y/%m/%d %H:%M")
        tg_send(chat_id, f"📊 현재 시황 {now}\n{'─'*38}\n" + "\n".join(lines))
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


def handle_help(chat_id: str) -> None:
    tg_send(chat_id,
        "👋 안녕하세요! 나만의 증시 비서입니다.\n\n"
        "📌 명령어:\n"
        "/market    — 현재 시황 즉시 요약\n"
        "/quant     — 퀀트 신호 + AI 종목 추천\n"
        "/portfolio — 보유 종목 현황 + 수익률\n"
        "/help      — 도움말\n\n"
        "📸 이미지 전송:\n"
        "• 주식 차트 → 기술적 분석\n"
        "• 보유종목 화면 → 종목별 전망\n\n"
        "💬 텍스트 질문:\n"
        "• 아무 질문이나 입력하면 AI가 답변\n"
        "• 예) '삼성전자 지금 사도 될까?'"
    )


COMMAND_HANDLERS = {
    "/quant":     lambda chat_id, args: handle_quant(chat_id, args),
    "/market":    lambda chat_id, args: handle_market(chat_id),
    "/portfolio": lambda chat_id, args: handle_portfolio(chat_id),
    "/start":     lambda chat_id, args: handle_help(chat_id),
    "/help":      lambda chat_id, args: handle_help(chat_id),
}


# ── 텍스트 AI 답변 ────────────────────────────────────────────

def handle_text_msg(chat_id: str, text: str) -> None:
    if not ANTHROPIC_API_KEY:
        tg_send(chat_id, "❌ AI 답변 기능을 사용하려면 ANTHROPIC_API_KEY가 필요합니다.")
        return
    tg_send(chat_id, "💭 분석 중...")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        reply = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=(
                "당신은 한국 주식/투자 전문 비서입니다. "
                "질문에 간결하고 구체적으로 답변하세요. "
                "수치와 근거를 제시하고, 마지막에 투자 결정은 본인 책임임을 한 줄로 언급하세요. "
                "한국어로 500자 내외로 답변하세요."
            ),
            messages=[{"role": "user", "content": text}],
        ).content[0].text
        tg_send(chat_id, f"💬 {reply}")
    except Exception as e:
        tg_send(chat_id, f"❌ AI 답변 실패: {e}")


# ── 사진 AI 분석 ──────────────────────────────────────────────

def handle_photo_msg(chat_id: str, file_id: str, caption: str = "") -> None:
    if not ANTHROPIC_API_KEY:
        tg_send(chat_id, "❌ 이미지 분석 기능을 사용하려면 ANTHROPIC_API_KEY가 필요합니다.")
        return
    tg_send(chat_id, "📸 이미지 분석 중... (30~60초 소요)")
    try:
        # 파일 경로 조회
        file_info = tg_get("getFile", file_id=file_id)
        file_path = file_info.get("result", {}).get("file_path", "")
        if not file_path:
            tg_send(chat_id, "❌ 이미지 파일을 가져올 수 없습니다.")
            return

        # 이미지 다운로드 → base64
        img_bytes  = requests.get(
            f"https://api.telegram.org/file/bot{TOKEN}/{file_path}", timeout=30
        ).content
        img_base64 = base64.b64encode(img_bytes).decode()
        mime_type  = "image/jpeg" if file_path.endswith(".jpg") else "image/png"

        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        prompt = (
            f"{'사용자 메모: ' + caption + chr(10) if caption else ''}"
            "이 주식 관련 이미지를 분석해주세요.\n\n"
            "이미지 유형에 따라:\n"
            "- 차트: 추세, 지지/저항, 주요 기술적 신호 분석\n"
            "- 보유종목 화면: 각 종목 현황 및 간단한 전망\n"
            "- 기타: 이미지 내용 설명 후 투자 관련 인사이트\n\n"
            "한국어로 명확하고 실용적으로 답변하세요. "
            "투자 결정은 본인 책임임을 마지막에 한 줄로 언급하세요."
        )

        reply = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime_type, "data": img_base64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        ).content[0].text

        tg_send(chat_id, f"📊 {reply}")
    except Exception as e:
        tg_send(chat_id, f"❌ 이미지 분석 실패: {e}")


# ── 메인 ─────────────────────────────────────────────────────

def main():
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN 미설정")
        sys.exit(1)

    me = tg_get("getMe")
    if not me.get("ok"):
        print(f"[ERROR] 봇 토큰 오류: {me}")
        sys.exit(1)
    print(f"[OK] 봇 연결: @{me['result'].get('username')}")

    tg_get("deleteWebhook")

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 메시지 확인 시작...")
    all_msgs = get_recent_messages()

    if not all_msgs:
        print("  → 처리할 메시지 없음")
        return

    # 타입별로 최신 1개씩만 처리 (중복 방지)
    seen   = set()
    unique = []
    for m in sorted(all_msgs, key=lambda x: x["ts"], reverse=True):
        key = m["type"] if m["type"] != "command" else m["text"].split()[0].lower().split("@")[0]
        if key not in seen:
            seen.add(key)
            unique.append(m)

    for m in unique:
        chat_id = m["chat_id"]
        mtype   = m["type"]

        if mtype == "command":
            parts   = m["text"].split()
            command = parts[0].lower().split("@")[0]
            args    = parts[1:]
            print(f"  → 명령어: {command} {args}")
            handler = COMMAND_HANDLERS.get(command)
            if handler:
                handler(chat_id, args)
            else:
                print(f"  → 미지원 명령어: {command}")

        elif mtype == "text":
            print(f"  → 텍스트 AI: '{m['text'][:30]}'")
            handle_text_msg(chat_id, m["text"])

        elif mtype == "photo":
            print(f"  → 사진 분석: file_id={m['file_id'][:20]}...")
            handle_photo_msg(chat_id, m["file_id"], m.get("text", ""))

    print("  → 완료")


if __name__ == "__main__":
    main()
