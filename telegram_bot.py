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
import threading
from datetime import datetime, timezone, timedelta, time as dtime
from http.server import HTTPServer, BaseHTTPRequestHandler
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
from stock_research import research_stock, fetch_korean_news


# ── 메시지 분할 & 안전 전송 헬퍼 ────────────────────────────────

def smart_split(text: str, max_len: int = 1800) -> list[str]:
    """줄바꿈 단위로 자연스럽게 분할 — 단락 중간에서 자르지 않음."""
    if len(text) <= max_len:
        return [text]

    chunks, current, current_len = [], [], 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if current_len + line_len > max_len and current:
            chunks.append("\n".join(current))
            current, current_len = [line], line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


async def safe_send(bot_msg, text: str,
                    edit: bool = False,
                    user_msg=None,
                    context=None) -> None:
    """
    분할 + 안전 전송.
    - 첫 청크: bot_msg.edit_text (edit=True) 또는 send_message
    - 후속 청크: context.bot.send_message() 직접 호출 (가장 안정적)
    - 청크 간 0.8초 딜레이
    """
    import asyncio

    chunks  = smart_split(text)
    chat_id = bot_msg.chat_id

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.8)

        try:
            if i == 0 and edit:
                await bot_msg.edit_text(chunk)
            elif context:
                # context.bot.send_message 가 가장 안정적
                await context.bot.send_message(chat_id=chat_id, text=chunk)
            else:
                await bot_msg.reply_text(chunk)
        except Exception as e:
            print(f"[safe_send] 청크 {i} 실패 ({len(chunk)}자): {e}")
            # 최후 수단: bot_msg 에서 reply
            try:
                await bot_msg.reply_text(chunk)
            except Exception as e2:
                print(f"[safe_send] 청크 {i} 최후 재시도 실패: {e2}")


# ── 대화 메모리 (채팅별 최근 5회 기억) ────────────────────────

from collections import defaultdict

_conv: dict[int, list[dict]] = defaultdict(list)
_MAX_TURNS = 5   # 최근 5회 질문/답변 유지


def _history(chat_id: int) -> list[dict]:
    return list(_conv[chat_id])


def _save(chat_id: int, role: str, content: str) -> None:
    _conv[chat_id].append({"role": role, "content": content})
    # 최대 turns*2 개 유지
    if len(_conv[chat_id]) > _MAX_TURNS * 2:
        _conv[chat_id] = _conv[chat_id][-_MAX_TURNS * 2:]


def _clear(chat_id: int) -> None:
    _conv[chat_id] = []


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
AUTHORIZED_CHATS = {
    s.strip() for s in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if s.strip()
}
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
NEWS_API_KEY     = os.getenv("NEWS_API_KEY", "")


# ── 보안: 허가된 사용자만 응답 ────────────────────────────────

def is_authorized(update: Update) -> bool:
    cid = str(update.effective_chat.id)
    if cid not in AUTHORIZED_CHATS:
        name = getattr(update.effective_chat, "full_name", "?") or "?"
        print(f"[UNAUTHORIZED] chat_id={cid} name={name}")
        # 주인(첫 번째 등록 ID)에게 텔레그램 알림 발송
        try:
            owner_id = next(iter(AUTHORIZED_CHATS), "")
            if owner_id and TELEGRAM_BOT_TOKEN:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": owner_id,
                          "text": f"[새 접속 시도]\nchat_id: {cid}\n이름: {name}\n\n이 ID를 허용하려면 Render 환경변수 TELEGRAM_CHAT_ID에 추가하세요."},
                    timeout=5,
                )
        except Exception:
            pass
        return False
    return True


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
    _clear(update.effective_chat.id)   # 새 대화 시작 시 히스토리 초기화
    await update.message.reply_text(
        "👋 *안녕하세요! 나만의 증시 비서입니다.*\n\n"
        "📸 *이미지 전송:*\n"
        "• MTS 보유종목 화면 → 종목별 향후 전망 & 투자전략\n"
        "• 주식 차트 화면 → 기술적 분석 (일목균형표, 지지/저항)\n\n"
        "💬 *텍스트 질문:*\n"
        "• 아무 질문이나 입력하면 시황 반영 답변\n"
        "• 예) '삼성전자 지금 사도 될까?' '나스닥 전망은?'\n\n"
        "📌 *명령어:*\n"
        "/market    — 현재 시황 즉시 요약\n"
        "/brief     — 모닝 브리핑 즉시 실행\n"
        "/quant     — 퀀트 신호 + AI 종목 추천\n"
        "  예) /quant KOSDAQ 15\n"
        "/portfolio — 보유 종목 현황 + 수익률\n"
        "/rebalance — 리밸런싱 계산\n"
        "  예) /rebalance 10 apply  (저장까지)\n"
        "/help      — 도움말",
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
    kr_news    = fetch_korean_news(n=5)
    news_text  = (
        "[해외 뉴스]\n" + "\n".join(news_list[:4])
        + "\n\n[국내 뉴스]\n" + "\n".join(kr_news)
        if (news_list or kr_news) else "뉴스 없음"
    )

    prompt = f"""당신은 한국 주식시장 전문 애널리스트입니다.
아래 현재 시장 데이터와 해외·국내 뉴스를 모두 반영하여 지금 이 순간의 시황을 분석해주세요.

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
    await safe_send(msg, full, edit=True, user_msg=update.message, context=context)


# ── 자동 모닝 브리핑 (매일 07:50 KST) ───────────────────────

KST = timezone(timedelta(hours=9))

async def job_morning_brief(context) -> None:
    """Railway job_queue 가 매일 07:50 KST 에 자동 호출"""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "morning_brief",
            Path(__file__).resolve().parent / "morning_brief.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run_morning_brief()
    except Exception as e:
        try:
            await context.bot.send_message(
                chat_id=int(AUTHORIZED_CHAT),
                text=f"⚠️ 자동 브리핑 실패: {e}"
            )
        except Exception:
            pass


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
                    '"indicators":"보이는 지표 (chart일 때, 예: 일목균형표,MACD,볼린저밴드)",'
                    '"bb_position":"볼린저밴드 위치 (상단근처/중간/하단근처/하단이탈/없음)"}'
                )}
            ]
        }]
    )

    try:
        raw      = detect.content[0].text.strip().replace("```json","").replace("```","")
        detected = json.loads(raw)
    except Exception:
        detected = {"type": "other", "stocks": [], "chart_stock": "", "indicators": "", "bb_position": "없음"}

    img_type     = detected.get("type", "other")
    stocks       = detected.get("stocks", [])
    chart_stock  = detected.get("chart_stock", "")
    indicators   = detected.get("indicators", "")
    bb_position  = detected.get("bb_position", "없음")

    # ── Step 2: 유형별 분석 ──

    if img_type == "portfolio" and stocks:
        # ── 증권사 리포트 + 뉴스 수집 (종목별) ──
        await msg.edit_text("📊 증권사 리포트 및 뉴스 수집 중...")
        research_parts = []
        for s in stocks[:6]:
            data = research_stock(s)
            if data and "리서치 데이터 없음" not in data:
                research_parts.append(data)
        research_text = "\n\n".join(research_parts) if research_parts else "수집된 리서치 데이터 없음"

        await msg.edit_text("🤖 AI 종합 분석 중... (30~50초)")

        prompt = f"""당신은 한국 주식 전문 애널리스트이자 투자 비서입니다.
보유 종목 화면을 보고, 아래 증권사 리포트·목표주가·뉴스 데이터를 바탕으로
각 종목에 대한 심층 분석을 제공하세요.

[보유 종목]
{', '.join(stocks)}

[현재 매크로 환경]
{macro_text}

[증권사 리포트 & 뉴스 데이터]
{research_text}

각 종목별로 아래 항목을 분석하세요:
① 증권가 컨센서스 요약
   - 주요 증권사 투자의견 및 목표주가 범위
   - 현재가 대비 목표주가 괴리율 (upside/downside %)
   - 최근 리포트의 핵심 투자 포인트
② 현재 업황 & 매크로와의 연관성
③ 기술적 분석 관점 (추세, 주요 지지/저항)
④ 밸류에이션 (Forward P/E, 업종 대비 수준)
⑤ 리스크 요인
⑥ 투자 전략 (단기 1개월 / 중기 3~6개월)

마지막에 전체 포트폴리오 관점에서 리스크 분산 및 오늘의 전략을 한 문단으로 요약.

텔레그램 메시지용 포맷 규칙:
- 마크다운 헤더(#, ##), 볼드(**), 수평선(---) 사용 금지
- 이모지 + 일반 텍스트로 구분
- 각 종목 핵심 위주로 상세하게 작성 (종목당 400자 내외, 자동으로 여러 메시지에 나눠 전송됨)
- 한국어로 작성."""

        analysis = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4000,
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

        # BB: yfinance 실제 계산 (종목 인식된 경우) → 시각 감지 대체
        bb_info = ""
        if chart_stock and "볼린저밴드" in (indicators or ""):
            try:
                from stock_research import get_kr_stock_code
                import yfinance as yf
                import numpy as np
                code = get_kr_stock_code(chart_stock)
                yf_ticker = None
                for sfx in [".KS", ".KQ"]:
                    t = yf.Ticker(f"{code}{sfx}" if code else f"{chart_stock}{sfx}")
                    hist = t.history(period="2mo", interval="1d")
                    if len(hist) >= 20:
                        yf_ticker = hist
                        break
                if yf_ticker is not None and len(yf_ticker) >= 20:
                    closes = yf_ticker["Close"]
                    ma20   = closes.rolling(20).mean().iloc[-1]
                    std20  = closes.rolling(20).std().iloc[-1]
                    bb_up  = ma20 + 2 * std20
                    bb_dn  = ma20 - 2 * std20
                    cur    = closes.iloc[-1]
                    pct_b  = (cur - bb_dn) / (bb_up - bb_dn) if (bb_up - bb_dn) > 0 else 0.5
                    if pct_b >= 0.8:
                        bb_pos_str = "상단 근처"
                    elif pct_b <= 0.2:
                        bb_pos_str = "하단 근처 (과매도)"
                    else:
                        bb_pos_str = "중간 구간"
                    bb_info = (
                        f"[실시간 볼린저밴드 데이터 (20일, 2σ)]\n"
                        f"BB 상단: {bb_up:,.0f}원 / BB 중간(MA20): {ma20:,.0f}원 / BB 하단: {bb_dn:,.0f}원\n"
                        f"현재가: {cur:,.0f}원 / %B: {pct_b:.2f} → {bb_pos_str}\n"
                        f"(이 수치를 분석의 기준으로 사용하고, 차트 시각 판단보다 우선 적용)"
                    )
            except Exception:
                if bb_position and bb_position != "없음":
                    bb_info = f"볼린저밴드 현재 위치(차트 시각 감지): {bb_position}"
        elif bb_position and bb_position != "없음":
            bb_info = f"볼린저밴드 현재 위치(차트 시각 감지): {bb_position}"

        prompt = f"""당신은 기술적 분석 전문가입니다.
이 차트를 세밀하게 분석하세요.

{stock_info}
{ind_info}
{bb_info}

[현재 매크로 환경]
{macro_text}

아래 순서로 자세히 분석하세요. 텔레그램 메시지용 포맷 규칙:
- 제목은 이모지 + 일반 텍스트 사용 (예: 📈 추세 분석)
- 마크다운 헤더(#, ##, ###), 볼드(**), 수평선(---) 절대 사용 금지
- 각 항목을 핵심 위주로 상세하게 작성 (전체 1500자 내외, 자동으로 여러 메시지에 나눠 전송됨)

📈 추세 분석
- 현재 주추세 (상승/하락/횡보)
- 단기/중기 추세선 상태
- 최근 고점/저점 패턴

☁️ 일목균형표 분석 (차트에 표시된 경우)
- 구름대 위/아래 여부 → 강세/약세 판단
- 전환선(9일)과 기준선(26일) 크로스 여부
- 후행스팬 위치 및 삼역 호전/역전 여부

🎯 지지 & 저항
- 강한 지지 구간 (가격대 명시)
- 강한 저항 구간 (가격대 명시)
- 돌파 시 다음 목표가

📊 보조 지표 (보이는 경우)
- 거래량 분석, RSI/MACD 상태

📉 볼린저 밴드 분석 (차트에 표시된 경우)
- 현재 가격의 밴드 위치 (상단/중간/하단 근처)
- 하단 밴드 터치 또는 이탈 여부
  → 하단 터치: 과매도 가능성, 반등 시도 구간인지 확인
  → 하단 이탈: 강한 하락 추세 or 일시적 과매도 판단
- 밴드 폭 (수축 중 / 확장 중)
  → 수축: 변동성 감소, 큰 방향성 이탈 임박 가능
  → 확장: 추세 강화 중
- %B 위치 (0 이하 = 하단 이탈, 1 이상 = 상단 이탈)
- 볼린저 밴드와 일목균형표/이평선 조합 시 시사점

🔮 향후 시나리오
- 상승 시나리오: 조건과 목표가
- 하락 시나리오: 조건과 손절선

💡 투자 전략
- 포지션 권고 (매수/관망/매도)
- 진입가 / 목표가 / 손절가

한국어로 작성."""

        analysis = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4000,
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
        title = f"📈 차트 기술적 분석 ({now})"
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

    tmp_path.unlink(missing_ok=True)

    # 대화 메모리에 저장 (후속 질문 연계용)
    chat_id = update.effective_chat.id
    _save(chat_id, "user", f"[이미지 분석 요청: {img_type}]")
    _save(chat_id, "assistant", reply[:800])   # 요약본만 저장

    await safe_send(msg, reply, edit=True, user_msg=update.message, context=context)


# ── 텍스트 질문 처리 ──────────────────────────────────────────

# 자주 묻는 미국 주요 티커
_US_TICKERS = {
    "AAPL","MSFT","NVDA","GOOGL","GOOG","AMZN","META","TSLA",
    "AMD","INTC","QCOM","AVGO","TSM","SMCI","MU","ARM",
    "NFLX","DIS","JPM","BAC","GS","V","MA","PYPL",
    "SPY","QQQ","SOXL","TQQQ",
}

def _detect_stocks_in_text(text: str) -> list[str]:
    """질문 텍스트에서 종목명 추출 (한국 + 미국)"""
    import re as _re
    from stock_research import KR_STOCK_MAP

    found = []
    # 한국 주식: 사전 기반
    for name in KR_STOCK_MAP:
        if name in text and name not in found:
            found.append(name)
    # 미국 주식: 대문자 2~5글자 티커
    for ticker in _re.findall(r'\b[A-Z]{2,5}\b', text):
        if ticker in _US_TICKERS and ticker not in found:
            found.append(ticker)
    return found[:4]   # 최대 4개


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    question = update.message.text.strip()
    chat_id  = update.effective_chat.id
    msg      = await update.message.reply_text("💭 분석 중...")

    # ── 매크로 & 뉴스 (해외 + 국내) ──
    macro      = get_macro_context()
    macro_text = format_macro(macro)
    news_list  = fetch_recent_news(n=4)
    kr_news    = fetch_korean_news(n=4)
    news_text  = (
        "[해외 뉴스]\n" + "\n".join(news_list)
        + ("\n\n[국내 뉴스]\n" + "\n".join(kr_news) if kr_news else "")
    )

    # ── 종목 감지 → 실시간 데이터 + 증권사 리포트 ──
    detected      = _detect_stocks_in_text(question)
    research_text = ""
    price_summary = ""   # 현재가 요약 (시스템 프롬프트 상단에 명시)
    if detected:
        await msg.edit_text(f"🔍 {', '.join(detected)} 데이터 수집 중...")
        parts = []
        for stock in detected:
            data = research_stock(stock)
            if data and "데이터 없음" not in data:
                parts.append(data)
                # 현재가 추출 → 상단 명시용
                for line in data.splitlines():
                    if "현재가:" in line:
                        price_summary += f"{stock} {line.strip()}\n"
                        break
        research_text = "\n\n".join(parts)

    # ── 매수/매도 판단 여부 ──
    is_buy_q = any(k in question for k in [
        "사도", "살까", "매수", "사야", "지금", "들어가", "진입",
        "팔아야", "팔까", "매도", "들고", "홀딩", "보유",
    ])
    verdict_guide = (
        """매수/매도 판단:
- 증권가 컨센서스 (목표주가, upside %)
- 52주 내 현재가 위치 (저점/고점 근접 여부)
- PER/PBR 업종 대비 수준
- 매크로 섹터 영향
- 마지막에 반드시: ✅매수 적극 고려 / ⚠️신중 접근 / ❌현재 비추천"""
        if (is_buy_q and detected) else
        "질문의 핵심에 직접 답변하고 수치와 근거를 제시하세요."
    )

    # ── 시스템 프롬프트 (매번 최신 매크로 반영) ──
    system_prompt = f"""당신은 한국 주식/투자 전문 비서입니다.
이전 대화 맥락을 기억하고 이어서 답변하세요.
{(chr(10) + "[현재가 (이 수치를 기준으로 모든 원 단위 계산)]" + chr(10) + price_summary) if price_summary else ""}
[현재 매크로 환경]
{macro_text}

[최근 주요 뉴스]
{news_text}
{"[종목 실시간 데이터 & 증권사 리포트]" + chr(10) + research_text if research_text else ""}

답변 지침:
{verdict_guide}
- 수치와 근거를 구체적으로 제시
- 매수/매도 타점·지지선·목표가 언급 시 반드시 실제 주가(원) 계산해서 표기
  예) 현재가 200,000원 기준 "-3% 구간 → 약 194,000원", "20일선 지지 → 약 190,000원대"
  반드시 위 현재가를 기준으로 직접 계산하여 원 단위로 표기할 것. 퍼센트(%)만 쓰면 안 됨
- 절대 사용자에게 현재가를 물어보지 말 것. 현재가는 위에 제공됨
- 이전 대화에서 언급된 종목/주제가 있으면 자연스럽게 연결
- 투자 결정은 본인 책임임을 마지막 한 줄에 언급
- 텔레그램 일반 텍스트 메시지 규칙: 마크다운 헤더(#, ##, ###), 볼드(**), 기울임(*), 수평선(---), 표(|) 절대 사용 금지. 이모지 + 일반 텍스트로만 구성
- 한국어, 800자 내외 (길면 자동으로 여러 메시지로 분할됨)"""

    # ── 대화 히스토리 + 현재 질문으로 Claude 호출 ──
    history  = _history(chat_id)
    messages = history + [{"role": "user", "content": question}]

    reply = claude().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1800,
        system=system_prompt,
        messages=messages,
    ).content[0].text

    # ── 히스토리 저장 ──
    _save(chat_id, "user",      question)
    _save(chat_id, "assistant", reply)

    await safe_send(msg, f"💬 {reply}", edit=True, user_msg=update.message, context=context)


# ── /portfolio (보유 현황) ────────────────────────────────────

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = await update.message.reply_text("⏳ 포트폴리오 현황 조회 중...")
    try:
        from quant_portfolio import get_portfolio_status, next_rebalance_date
        status = get_portfolio_status()
        next_r = next_rebalance_date()
        full   = f"{status}\n\n📅 다음 리밸런싱 권장일: {next_r}"
        await safe_send(msg, full, edit=True, user_msg=update.message, context=context)
    except Exception as e:
        await msg.edit_text(f"❌ 오류: {e}")


# ── /rebalance (리밸런싱 실행) ────────────────────────────────

async def cmd_rebalance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    args_list = context.args  # /rebalance [top_n] [apply]
    top_n = 10
    apply = False
    for arg in (args_list or []):
        if arg.isdigit():
            top_n = int(arg)
        elif arg.lower() == "apply":
            apply = True

    msg = await update.message.reply_text(
        f"⏳ 리밸런싱 계산 중... (상위 {top_n}종목)\n"
        + ("✅ apply 옵션: 완료 후 포트폴리오 자동 저장" if apply else
           "ℹ️ 계산만 합니다. 실제 저장하려면 /rebalance apply")
    )

    try:
        from quant_portfolio import compute_rebalance, apply_rebalance, format_rebalance

        rebal = compute_rebalance(top_n=top_n)

        if apply and "error" not in rebal:
            apply_rebalance(rebal)
            saved_note = "\n\n✅ 포트폴리오가 저장되었습니다. /portfolio 로 확인하세요."
        else:
            saved_note = "\n\n💡 실제 저장: /rebalance apply"

        result_text = format_rebalance(rebal)
        now  = datetime.now().strftime("%Y/%m/%d %H:%M")
        full = (
            f"🔄 *리밸런싱 검토* `{now}`\n"
            f"{'─'*36}\n"
            f"{result_text}"
            f"{saved_note}"
        )
        await safe_send(msg, full, edit=True, user_msg=update.message, context=context)

    except Exception as e:
        await msg.edit_text(f"❌ 리밸런싱 오류: {e}")


# ── /quant (퀀트 신호) ────────────────────────────────────────

async def cmd_quant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    args_list = context.args
    top_n = 10
    for arg in (args_list or []):
        if arg.isdigit():
            top_n = int(arg)

    chat_id = str(update.effective_chat.id)

    await update.message.reply_text(
        f"⏳ 퀀트 신호 분석 중... (1~2분 소요)\n상위 {top_n}개 종목 팩터 스코어링 + AI 추천"
    )

    github_pat = os.getenv("GITHUB_PAT", "")
    if not github_pat:
        await update.message.reply_text("❌ GITHUB_PAT 미설정 — 관리자에게 문의하세요.")
        return

    resp = requests.post(
        "https://api.github.com/repos/myfeel1024/morning-brief/actions/workflows/bot_polling.yml/dispatches",
        headers={
            "Authorization": f"token {github_pat}",
            "Accept": "application/vnd.github.v3+json",
        },
        json={"ref": "main", "inputs": {"chat_id": chat_id, "top_n": str(top_n)}},
        timeout=10,
    )
    if resp.status_code != 204:
        await update.message.reply_text(f"❌ 퀀트 분석 트리거 실패 (status={resp.status_code})")


# ── 봇 실행 ───────────────────────────────────────────────────

def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_start))
    app.add_handler(CommandHandler("market",    cmd_market))
    app.add_handler(CommandHandler("brief",     cmd_brief))
    app.add_handler(CommandHandler("quant",     cmd_quant))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("rebalance", cmd_rebalance))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # ── 매일 07:50 KST 자동 모닝 브리핑 ──
    app.job_queue.run_daily(
        job_morning_brief,
        time=dtime(hour=7, minute=50, second=0, tzinfo=KST),
        name="morning_brief_daily",
    )

    # Render Web Service 슬립 방지용 헬스체크 서버
    port = int(os.getenv("PORT", 10000))

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, *args):
            pass

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), _Health).serve_forever(),
        daemon=True,
    ).start()
    print(f"[Health] :{port} 헬스체크 서버 시작")

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_kst} KST] 증시 비서 봇 시작! (모닝 브리핑: 매일 07:50 KST)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
