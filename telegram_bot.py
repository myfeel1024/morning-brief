"""
====================================================
  📱 증시 비서 텔레그램 봇 (양방향 인터랙티브)
====================================================
기능:
  - MTS 보유종목 스크린샷 → 종목별 투자 전략 분석
  - 주식 차트 스크린샷 → 일목균형표/기술적 분석
  - 텍스트 질문 → 현재 시황 반영 AI 답변
  - /market  → 즉시 시황 요약
  - /brief   → 모닝 브리핑 수동 실행

설치:
  pip install "python-telegram-bot[job-queue]" yfinance anthropic requests
====================================================
"""

import os
import base64
import json
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile

import yfinance as yf
import requests
import anthropic
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)


# ── 환경변수 로드 ─────────────────────────────────────────────

def _load_env_file():
    """스크립트 위치 기준으로 .env 파일 직접 파싱"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ[key] = value

_load_env_file()

TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")   # 허가된 채팅 ID (보안)
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
NEWS_API_KEY     = os.getenv("NEWS_API_KEY", "")


# ── 보안: 허가된 사용자만 응답 ────────────────────────────────

def is_authorized(update: Update) -> bool:
    return str(update.effective_chat.id) == str(AUTHORIZED_CHAT)


# ── 매크로 데이터 수집 ────────────────────────────────────────

MACRO_TICKERS = {
    "S&P500"    : "^GSPC",
    "Nasdaq"    : "^IXIC",
    "Dow Jones" : "^DJI",
    "VIX"       : "^VIX",
    "미10년물금리": "^TNX",
    "달러인덱스"  : "DX-Y.NYB",
    "WTI유가"   : "CL=F",
    "금"         : "GC=F",
    "달러/원"    : "USDKRW=X",
    "한국ETF(EWY)": "EWY",
}

def get_macro_context() -> dict:
    data = {}
    for name, sym in MACRO_TICKERS.items():
        try:
            info  = yf.Ticker(sym).fast_info
            price = info.last_price
            prev  = info.previous_close
            if price and prev:
                pct = (price - prev) / prev * 100
                data[name] = {"price": price, "pct": pct}
        except Exception:
            pass
    return data

def format_macro(data: dict) -> str:
    lines = []
    for name, d in data.items():
        arrow = "▲" if d["pct"] >= 0 else "▼"
        sign  = "+" if d["pct"] >= 0 else ""
        lines.append(f"{name:<14}: {d['price']:>10,.2f}  {arrow}{sign}{d['pct']:.2f}%")
    return "\n".join(lines)


# ── Claude 클라이언트 ─────────────────────────────────────────

def claude():
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)

def image_to_base64(path: str) -> tuple[str, str]:
    ext  = path.rsplit(".", 1)[-1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png",  "webp": "image/webp"}.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode()
    return data, mime


# ── 뉴스 수집 ─────────────────────────────────────────────────

def fetch_recent_news(query: str = None, n: int = 5) -> list[str]:
    if not NEWS_API_KEY:
        return []
    if query is None:
        query = (
            "Fed OR inflation OR \"interest rate\" OR CPI "
            "OR war OR Ukraine OR tariff OR sanctions OR geopolitics"
        )
    trusted = (
        "reuters.com,apnews.com,bloomberg.com,wsj.com,"
        "ft.com,cnbc.com,marketwatch.com,economist.com,"
        "axios.com,politico.com,thehill.com,npr.org"
    )
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    url = (
        "https://newsapi.org/v2/everything"
        f"?q={requests.utils.quote(query)}"
        f"&domains={trusted}"
        f"&from={yesterday}&language=en&sortBy=relevancy"
        f"&pageSize={n}&apiKey={NEWS_API_KEY}"
    )
    try:
        res  = requests.get(url, timeout=10)
        data = res.json()
        return [
            f"• {a['title']} ({a.get('source',{}).get('name','')})"
            for a in data.get("articles", []) if a.get("title")
        ]
    except Exception:
        return []


# ── /start, /help ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "👋 *안녕하세요! 나만의 증시 비서입니다.*\n\n"
        "📸 *이미지 전송:*\n"
        "• MTS 보유종목 화면 → 종목별 향후 전망 & 투자전략\n"
        "• 주식 차트 화면 → 기술적 분석 (일목균형표, 지지/저항)\n\n"
        "💬 *텍스트 질문:*\n"
        "• 아무 질문이나 입력하면 시황 반영 답변\n"
        "• 예) '삼성전자 지금 사도 될까?' '나스닥 전망은?'\n\n"
        "📌 *명령어:*\n"
        "/market — 현재 시황 즉시 요약\n"
        "/brief  — 모닝 브리핑 즉시 실행\n"
        "/help   — 도움말",
        parse_mode="Markdown",
    )


# ── /market ───────────────────────────────────────────────────

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = await update.message.reply_text("⏳ 현재 시황 분석 중...")

    macro      = get_macro_context()
    macro_text = format_macro(macro)
    news_list  = fetch_recent_news()
    news_text  = "\n".join(news_list[:5]) if news_list else "뉴스 없음"

    prompt = f"""당신은 한국 주식시장 전문 애널리스트입니다.
아래 현재 시장 데이터와 뉴스를 바탕으로 지금 이 순간의 시황을 분석해주세요.

[현재 시장 지표]
{macro_text}

[주요 뉴스]
{news_text}

다음 형식으로 한국어 작성 (500자 이내):
① 현재 시장 분위기 한 줄 요약
② 주목할 매크로 포인트 (금리/달러/유가 중 핵심 1~2개)
③ 코스피 투자자 관점의 시사점
④ 지금 당장 주의할 리스크"""

    reply_text = claude().messages.create(
        model="claude-sonnet-4-6", max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    ).content[0].text

    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    full = (
        f"📊 *현재 시황* `{now}`\n"
        f"```\n{macro_text}\n```\n\n"
        f"🤖 *AI 분석*\n{reply_text}"
    )
    await msg.edit_text(full, parse_mode="Markdown")


# ── /brief (모닝 브리핑 수동 실행) ───────────────────────────

async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ 모닝 브리핑 생성 중... (약 60초)")
    try:
        # morning_brief 모듈 동적 임포트
        import importlib, sys
        spec = importlib.util.spec_from_file_location(
            "morning_brief",
            Path(__file__).resolve().parent / "morning_brief.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run_morning_brief()
        await update.message.reply_text("✅ 브리핑 전송 완료!")
    except Exception as e:
        await update.message.reply_text(f"❌ 브리핑 실패: {e}")


# ── 사진 메시지 처리 ──────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    msg = await update.message.reply_text("📸 이미지 분석 중... (30~60초 소요)")

    # 이미지 다운로드
    photo    = update.message.photo[-1]
    tg_file  = await context.bot.get_file(photo.file_id)
    tmp_path = Path(tempfile.mktemp(suffix=".jpg"))
    await tg_file.download_to_drive(str(tmp_path))

    img_data, mime_type = image_to_base64(str(tmp_path))
    macro      = get_macro_context()
    macro_text = format_macro(macro)
    client     = claude()

    # ── Step 1: 이미지 유형 판별 ──
    detect = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime_type, "data": img_data
                }},
                {"type": "text", "text": (
                    "이 이미지의 유형을 판별하고 필요한 정보를 추출하세요.\n"
                    "유형:\n"
                    "- portfolio: 주식 보유종목/잔고 화면\n"
                    "- chart: 주식/지수 차트 화면\n"
                    "- other: 기타\n\n"
                    "JSON만 응답 (설명 없이):\n"
                    '{"type":"portfolio"|"chart"|"other",'
                    '"stocks":["종목1",...],'
                    '"chart_stock":"종목명 (chart일 때)",'
                    '"chart_period":"기간 (chart일 때, 예: 3개월)",'
                    '"indicators":"보이는 지표 (chart일 때, 예: 일목균형표,MACD)"}'
                )}
            ]
        }]
    )

    try:
        raw      = detect.content[0].text.strip().replace("```json","").replace("```","")
        detected = json.loads(raw)
    except Exception:
        detected = {"type": "other", "stocks": [], "chart_stock": "", "indicators": ""}

    img_type     = detected.get("type", "other")
    stocks       = detected.get("stocks", [])
    chart_stock  = detected.get("chart_stock", "")
    indicators   = detected.get("indicators", "")

    # ── Step 2: 유형별 분석 ──

    if img_type == "portfolio" and stocks:
        # 포트폴리오 분석
        news_query = " OR ".join(stocks[:3]) + " stock Korea"
        news_list  = fetch_recent_news(query=news_query, n=4)
        news_text  = "\n".join(news_list) if news_list else ""

        prompt = f"""당신은 한국 주식 전문 애널리스트이자 투자 비서입니다.
보유 종목 화면을 보고 각 종목에 대한 종합 분석을 제공하세요.

[보유 종목]
{', '.join(stocks)}

[현재 매크로 환경]
{macro_text}

[관련 뉴스]
{news_text}

각 종목별로 아래 항목을 분석하세요:
① 현재 업황 & 매크로와의 연관성
② 기술적 분석 관점 (현재 추세, 주요 지지/저항 수준)
③ 밸류에이션 (Forward P/E, 업종 대비 수준)
④ 리스크 요인
⑤ 단기(1개월)/중기(3~6개월) 투자 전략

마지막에 전체 포트폴리오 관점에서 리스크 분산 및 오늘의 전략 한 문단으로 요약.

한국어로 작성. 각 종목 300자 이내."""

        analysis = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=3000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime_type, "data": img_data
                    }},
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        now   = datetime.now().strftime("%Y/%m/%d %H:%M")
        reply = (
            f"💼 *보유 종목 분석* `{now}`\n"
            f"📋 종목: {', '.join(stocks)}\n"
            f"{'─'*30}\n"
            f"{analysis.content[0].text}"
        )

    elif img_type == "chart":
        # 차트 기술적 분석
        stock_info = f"종목: {chart_stock}" if chart_stock else ""
        ind_info   = f"확인된 지표: {indicators}" if indicators else ""

        prompt = f"""당신은 기술적 분석 전문가입니다.
이 차트를 세밀하게 분석하세요.

{stock_info}
{ind_info}

[현재 매크로 환경]
{macro_text}

다음 항목을 분석하세요:

📈 **추세 분석**
- 현재 주추세 (상승/하락/횡보)
- 단기/중기 추세선 상태
- 최근 고점/저점 패턴

☁️ **일목균형표 분석** (차트에 표시된 경우)
- 구름대(선행스팬) 위/아래 여부 → 강세/약세 판단
- 전환선(9일)과 기준선(26일) 크로스 여부
- 후행스팬 위치
- 삼역 호전/역전 여부

🎯 **지지 & 저항**
- 강한 지지 구간 (가격대 명시)
- 강한 저항 구간 (가격대 명시)
- 돌파 시 다음 목표가

📊 **보조 지표** (보이는 경우)
- 거래량 분석
- RSI/MACD/볼린저밴드 등 상태

🔮 **향후 시나리오**
- 상승 시나리오: 조건과 목표가
- 하락 시나리오: 조건과 손절선
- 가장 가능성 높은 시나리오

💡 **투자 전략**
- 현재 포지션 권고 (매수/관망/매도)
- 진입가 / 목표가 / 손절가

한국어로 작성."""

        analysis = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime_type, "data": img_data
                    }},
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        now   = datetime.now().strftime("%Y/%m/%d %H:%M")
        title = f"📈 *차트 기술적 분석* `{now}`"
        if chart_stock:
            title += f"\n종목: {chart_stock}"
        reply = f"{title}\n{'─'*30}\n{analysis.content[0].text}"

    else:
        # 기타 이미지
        analysis = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": mime_type, "data": img_data
                    }},
                    {"type": "text", "text": (
                        f"이 이미지에서 투자/시장 관련 정보를 분석하세요.\n\n"
                        f"현재 매크로 환경:\n{macro_text}\n\n"
                        "투자 관련 정보가 없다면 이미지 내용을 요약해주세요.\n"
                        "한국어로 작성."
                    )}
                ]
            }]
        )
        reply = f"🔍 *이미지 분석*\n\n{analysis.content[0].text}"

    # 메시지 분할 전송 (4000자 초과 시)
    tmp_path.unlink(missing_ok=True)

    chunks = [reply[i:i+4000] for i in range(0, len(reply), 4000)]
    await msg.edit_text(chunks[0], parse_mode="Markdown")
    for chunk in chunks[1:]:
        await update.message.reply_text(chunk, parse_mode="Markdown")


# ── 텍스트 질문 처리 ──────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    question   = update.message.text.strip()
    msg        = await update.message.reply_text("💭 분석 중...")
    macro      = get_macro_context()
    macro_text = format_macro(macro)
    news_list  = fetch_recent_news(n=4)
    news_text  = "\n".join(news_list) if news_list else ""

    prompt = f"""당신은 한국 주식/투자 전문 비서입니다.
사용자의 질문에 현재 시황을 반영하여 전문적으로 답변하세요.

[현재 매크로 환경]
{macro_text}

[최근 주요 뉴스]
{news_text}

[사용자 질문]
{question}

답변 지침:
- 현재 시장 환경과 연결해서 설명
- 특정 종목 질문이면 기술적/펀더멘털 양면 언급
- 금리/환율/유가 등 매크로 영향 포함
- 투자 결정은 본인 책임임을 마지막에 한 줄 언급
- 한국어, 600자 이내"""

    reply = claude().messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    ).content[0].text

    await msg.edit_text(f"💬 {reply}", parse_mode="Markdown")


# ── 봇 실행 ───────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_start))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("brief",  cmd_brief))
    app.add_handler(MessageHandler(filters.PHOTO,                  handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 증시 비서 봇 시작!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
