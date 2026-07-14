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
import re
import base64
import json
import asyncio

from datetime import datetime, timezone, timedelta, time as dtime
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
from econ_cycle import (
    get_econ_cycle, format_econ_report,
    save_econ_cache, load_econ_cache,
    load_last_phase, save_last_phase,
)


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
                    context=None,
                    parse_mode: str = None) -> None:
    """
    분할 + 안전 전송.
    - 첫 청크: bot_msg.edit_text (edit=True) 또는 send_message
    - 후속 청크: context.bot.send_message() 직접 호출 (가장 안정적)
    - 청크 간 0.8초 딜레이
    """
    import asyncio

    chunks  = smart_split(text)
    chat_id = bot_msg.chat_id
    pm_kw   = {"parse_mode": parse_mode} if parse_mode else {}

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(0.8)

        try:
            if i == 0 and edit:
                await bot_msg.edit_text(chunk, **pm_kw)
            elif context:
                await context.bot.send_message(chat_id=chat_id, text=chunk, **pm_kw)
            else:
                await bot_msg.reply_text(chunk, **pm_kw)
        except Exception as e:
            print(f"[safe_send] 청크 {i} 실패 ({len(chunk)}자): {e}")
            try:
                await bot_msg.reply_text(chunk, **pm_kw)
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

_ALERTS_FILE = Path(__file__).resolve().parent / "alerts.json"

def _load_alerts() -> dict:
    if _ALERTS_FILE.exists():
        try:
            return json.loads(_ALERTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 로컬 파일 없음 → Gist에서 복구 (재배포 직후 첫 실행)
    try:
        from gist_store import load_json
        data = load_json("alerts.json")
        if data and isinstance(data, dict):
            _ALERTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[alerts] Gist에서 복구 완료")
            return data
    except Exception:
        pass
    return {}

def _save_alerts(data: dict) -> None:
    _ALERTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # Gist 백업 (비동기 없이 동기 처리 — 알림 변경이 드물어 지연 무관)
    try:
        from gist_store import save_json
        save_json("alerts.json", data)
    except Exception:
        pass


TOKEN            = os.getenv("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_CHATS = {
    s.strip() for s in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if s.strip()
}
ANTHROPIC_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
NEWS_API_KEY     = os.getenv("NEWS_API_KEY", "")


# ── 보안: 허가된 사용자만 응답 ────────────────────────────────

_notified: set = set()   # 이미 알림 보낸 chat_id (프로세스 내 중복 방지)

def is_authorized(update: Update) -> bool:
    cid = str(update.effective_chat.id)
    if cid not in AUTHORIZED_CHATS:
        if cid not in _notified:
            _notified.add(cid)
            name = getattr(update.effective_chat, "full_name", "?") or "?"
            print(f"[UNAUTHORIZED] chat_id={cid} name={name}")
            try:
                owner_id = next(iter(AUTHORIZED_CHATS), "")
                if owner_id and TOKEN:
                    requests.post(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
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


# ── 외국인·기관 순매수 (pykrx) ───────────────────────────────

_investor_flow_broken = False   # pykrx 투자자별 거래 API가 KRX 로그인을 요구해 실패하면 이후 호출 생략

def get_investor_flow(stock_name: str) -> str:
    """최근 5거래일 외국인·기관 순매수 (한국 주식 전용). 실패 시 빈 문자열."""
    global _investor_flow_broken
    if _investor_flow_broken:
        return ""
    try:
        from pykrx import stock as pk
        from stock_research import get_kr_stock_code
        code = get_kr_stock_code(stock_name)
        if not code:
            return ""
        end   = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        df = pk.get_market_trading_value_by_investor(start, end, code)
        if df is None or df.empty:
            # pykrx 내부에서 KRX 로그인 요구 등으로 빈 결과만 돌려주는 경우도 동일 처리
            _investor_flow_broken = True
            return ""
        df = df.tail(5)
        foreign     = df.get("외국인합계", df.get("외국인", None))
        institution = df.get("기관합계",   df.get("기관",   None))
        if foreign is None or institution is None:
            return ""
        f_val = foreign.sum() / 1e8
        i_val = institution.sum() / 1e8
        f_str = f"{'매수' if f_val >= 0 else '매도'} {abs(f_val):.0f}억"
        i_str = f"{'매수' if i_val >= 0 else '매도'} {abs(i_val):.0f}억"
        return f"[최근5일 수급] 외국인 {f_str} / 기관 {i_str}"
    except Exception as e:
        print(f"[investor_flow] 비활성화 (사유: {e})", flush=True)
        _investor_flow_broken = True
        return ""


# ── 가격 알림 — 현재가 조회 ───────────────────────────────────

def _get_yf_price(name_or_ticker: str) -> tuple[str, float]:
    """종목명 또는 티커 → (yfinance ticker 문자열, 현재가). 실패 시 ('', 0.0)."""
    from stock_research import get_kr_stock_code
    stripped = name_or_ticker.strip()
    # 미국 티커: 대문자 알파벳 1~5자 (BRK-B 등 하이픈 포함 허용)
    if re.match(r'^[A-Z]{1,5}(-[A-Z])?$', stripped):
        try:
            price = yf.Ticker(stripped).fast_info.last_price
            return (stripped, float(price)) if price else ("", 0.0)
        except Exception:
            return "", 0.0
    # 한국 주식: 이름 → 종목코드 → .KS/.KQ
    code = get_kr_stock_code(stripped)
    if not code:
        return "", 0.0
    for sfx in [".KS", ".KQ"]:
        try:
            price = yf.Ticker(f"{code}{sfx}").fast_info.last_price
            if price:
                return f"{code}{sfx}", float(price)
        except Exception:
            continue
    return "", 0.0


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
        cid  = update.effective_chat.id
        name = getattr(update.effective_chat, "full_name", "") or ""
        await update.message.reply_text(
            f"👋 안녕하세요{', ' + name if name else ''}!\n\n"
            f"🔑 내 Chat ID: {cid}\n\n"
            "이 숫자를 봇 관리자에게 알려주시면 접근 권한을 받을 수 있습니다."
        )
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
        "/market       — 현재 시황 즉시 요약\n"
        "/brief        — 모닝 브리핑 즉시 실행\n"
        "/quant        — 한국 퀀트 신호 (코스피·코스닥)\n"
        "  예) /quant 15\n"
        "/quant\\_us   — 미국 퀀트 신호 (S\\&P500·섹터)\n"
        "  예) /quant\\_us 15\n"
        "/alert        — 가격 알림 설정·조회·취소\n"
        "  예) /alert 삼성전자 70000\n"
        "  예) /alert AAPL 200\n"
        "  예) /alert list\n"
        "  예) /alert cancel 1\n"
        "/econ         — 미국 경기 국면 분석\n"
        "  (선행·동행·후행지표 → 회복/성장/둔화/침체 판단)\n"
        "  매월 말일 08:00 자동 브리핑\n"
        "/feargreed    — CNN 공포·탐욕 지수 (실시간)\n"
        "/help         — 도움말\n\n"
        "💬 *대화체도 됩니다:*\n"
        "• 한국 퀀트 알려줘\n"
        "• 미국 퀀트 알려줘\n"
        "• 경기지표 알려줘\n"
        "• 공포탐욕지수 알려줘\n"
        "• 삼성전자 7만원 넘으면 알려줘",
        parse_mode="Markdown",
    )


# ── /market ───────────────────────────────────────────────────

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = await update.message.reply_text("⏳ 현재 시황 분석 중...")

    def _build_market_reply() -> tuple[str, str]:
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
            messages=[{"role": "user", "content": prompt}],
            timeout=60,
        ).content[0].text
        return macro_text, reply_text

    try:
        loop = asyncio.get_event_loop()
        macro_text, reply_text = await asyncio.wait_for(
            loop.run_in_executor(None, _build_market_reply), timeout=90,
        )
    except asyncio.TimeoutError:
        await msg.edit_text("❌ 시황 분석 시간 초과 (90초). 잠시 후 다시 시도해주세요.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ 오류: {e}")
        return

    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    full = (
        f"📊 *현재 시황* `{now}`\n"
        f"```\n{macro_text}\n```\n\n"
        f"🤖 *AI 분석*\n{reply_text}"
    )
    await safe_send(msg, full, edit=True, user_msg=update.message, context=context, parse_mode="Markdown")


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
        for cid in AUTHORIZED_CHATS:
            try:
                await context.bot.send_message(chat_id=int(cid), text=f"⚠️ 자동 브리핑 실패: {e}")
            except Exception:
                pass


# ── /brief (모닝 브리핑 수동 실행) ───────────────────────────

async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text("⏳ 모닝 브리핑 생성 중... (약 60초)")
    try:
        import importlib
        spec = importlib.util.spec_from_file_location(
            "morning_brief",
            Path(__file__).resolve().parent / "morning_brief.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        requester = str(update.effective_chat.id)
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: mod.run_morning_brief(send_to=[requester])),
            timeout=180,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 브리핑 생성 시간 초과 (3분). 잠시 후 다시 시도해주세요.")
    except Exception as e:
        await update.message.reply_text(f"❌ 브리핑 실패: {e}")


# ── 가격 알림 — 한국어 숫자 파싱 & 공통 등록 로직 ──────────────

def _parse_korean_price(text: str) -> float | None:
    """'7만원', '7만5천', '70,000', '200.5' 등 → float. 실패 시 None."""
    t = re.sub(r'[원달러불$,\s]', '', text.strip())
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        pass
    total = 0.0
    found = False
    for pat, mul in [
        (r'(\d+\.?\d*)조', 1_000_000_000_000),
        (r'(\d+\.?\d*)억', 100_000_000),
        (r'(\d+\.?\d*)만', 10_000),
        (r'(\d+\.?\d*)천', 1_000),
        (r'(\d+\.?\d*)백', 100),
    ]:
        m = re.search(pat, t)
        if m:
            total += float(m.group(1)) * mul
            found = True
    return total if found else None


_ABOVE_WORDS          = ["넘으면", "초과", "올라가면", "오르면", "이상", "돌파", "달성"]
_BELOW_WORDS          = ["떨어지면", "내려가면", "이하", "하락", "빠지면", "밑으로"]
_ALERT_NATURAL_WORDS  = ["알려줘", "알림", "알려", "등록해줘"]

_PRICE_RE = re.compile(
    r'\d+\.?\d*\s*(?:조|억|만|천|백)(?:\s*\d+\.?\d*\s*(?:억|만|천|백))*\s*원?'
    r'|\d[\d,]+\s*원'
    r'|\d+\.?\d*\s*(?:달러|불|\$)'
    r'|\d[\d,]+'
)


async def _do_register_alert(update, name: str, target: float, direction: str | None) -> None:
    """알림 등록 공통 로직 (cmd_alert·handle_text 공유)."""
    chat_id = str(update.effective_chat.id)
    await update.message.reply_text(f"⏳ {name} 현재가 조회 중...")
    yf_ticker, current = _get_yf_price(name)
    if not yf_ticker:
        await update.message.reply_text(f"❌ '{name}' 종목을 찾을 수 없습니다.")
        return
    if direction is None:
        direction = "above" if target > current else "below"
    alerts      = _load_alerts()
    user_alerts = alerts.get(chat_id, [])
    new_id      = max((a["id"] for a in user_alerts), default=0) + 1
    user_alerts.append({
        "id": new_id, "name": name, "ticker": yf_ticker,
        "target": target, "direction": direction,
        "created_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
    })
    alerts[chat_id] = user_alerts
    _save_alerts(alerts)
    cur   = "원" if yf_ticker.endswith((".KS", ".KQ")) else "$"
    d_str = "이상 도달 시" if direction == "above" else "이하 하락 시"
    await update.message.reply_text(
        f"✅ 가격 알림 등록!\n\n"
        f"종목: {name}\n"
        f"현재가: {cur}{current:,.0f}\n"
        f"조건: {cur}{target:,.0f} {d_str} 알림\n"
        f"알림 번호: {new_id}"
    )


# ── /alert (가격 알림) ────────────────────────────────────────

async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    chat_id = str(update.effective_chat.id)
    args    = context.args or []

    HELP = (
        "📌 가격 알림 사용법\n\n"
        "/alert 삼성전자 70000  — 70,000원 도달 시 알림\n"
        "/alert AAPL 200        — $200 도달 시 알림\n"
        "/alert list            — 등록된 알림 목록\n"
        "/alert cancel 1        — 1번 알림 취소"
    )

    if not args:
        await update.message.reply_text(HELP)
        return

    alerts      = _load_alerts()
    user_alerts = alerts.get(chat_id, [])

    # ─ list
    if args[0].lower() in ("list", "목록"):
        if not user_alerts:
            await update.message.reply_text("등록된 알림이 없습니다.")
            return
        lines = ["📋 등록된 가격 알림\n"]
        for a in user_alerts:
            d_str = "이상 도달" if a["direction"] == "above" else "이하 하락"
            cur   = "원" if a["ticker"].endswith((".KS", ".KQ")) else "$"
            lines.append(f"{a['id']}. {a['name']}  {cur}{a['target']:,} {d_str}")
        await update.message.reply_text("\n".join(lines))
        return

    # ─ cancel
    if args[0].lower() in ("cancel", "취소", "삭제") and len(args) >= 2:
        cancel_arg = " ".join(args[1:])
        before = len(user_alerts)
        if cancel_arg.isdigit():
            user_alerts = [a for a in user_alerts if a["id"] != int(cancel_arg)]
            desc = f"{cancel_arg}번"
        else:
            user_alerts = [a for a in user_alerts if cancel_arg not in a["name"]]
            desc = f"'{cancel_arg}'"
        alerts[chat_id] = user_alerts
        _save_alerts(alerts)
        removed = before - len(user_alerts)
        msg = f"✅ {desc} 알림 {removed}개 취소 완료." if removed else f"❌ {desc} 알림을 찾을 수 없습니다."
        await update.message.reply_text(msg)
        return

    # ─ 신규 등록
    if len(args) < 2:
        await update.message.reply_text(HELP)
        return

    # 가격 파싱 — 숫자("70000") 또는 한국어("7만원", "7만5천") 모두 허용
    raw_price = " ".join(args[1:])   # "/alert 삼성전자 7만 5천" 처럼 띄어쓴 경우 합치기
    target = _parse_korean_price(raw_price)
    if not target or target <= 0:
        await update.message.reply_text("가격을 인식하지 못했습니다.\n예) /alert 삼성전자 70000\n예) /alert 삼성전자 7만원")
        return

    direction = None   # _do_register_alert 에서 현재가와 비교해 자동 판단
    await _do_register_alert(update, args[0], target, direction)


# ── 가격 알림 체크 잡 (5분마다) ──────────────────────────────

async def job_check_alerts(context) -> None:
    alerts = _load_alerts()
    if not alerts:
        return
    changed = False
    for chat_id, user_alerts in list(alerts.items()):
        remaining = []
        for a in user_alerts:
            try:
                price = yf.Ticker(a["ticker"]).fast_info.last_price
                if not price:
                    remaining.append(a)
                    continue
                hit = (
                    (a["direction"] == "above" and price >= a["target"]) or
                    (a["direction"] == "below" and price <= a["target"])
                )
                if hit:
                    cur   = "원" if a["ticker"].endswith((".KS", ".KQ")) else "$"
                    d_str = "🔺 목표가 돌파" if a["direction"] == "above" else "🔻 목표가 하락"
                    await context.bot.send_message(
                        chat_id=int(chat_id),
                        text=(
                            f"🔔 가격 알림!\n\n"
                            f"종목: {a['name']}\n"
                            f"{d_str}\n"
                            f"목표가: {cur}{a['target']:,.0f}\n"
                            f"현재가: {cur}{price:,.0f}"
                        ),
                    )
                    changed = True
                else:
                    remaining.append(a)
            except Exception:
                remaining.append(a)
        alerts[chat_id] = remaining
    if changed:
        _save_alerts(alerts)


# ── 경기 국면 분석 ────────────────────────────────────────────

def _get_phase_quant_picks(phase: str) -> str:
    """경기 국면 추천 티커 중 BUY 신호만 퀀트 점수 적용 → 포맷 문자열 반환."""
    try:
        from econ_cycle import _PHASE_TICKERS
        from quant_us import score_tickers_quick

        sectors = _PHASE_TICKERS.get(phase, [])
        if not sectors:
            return ""
        all_tickers, ticker_to_sector = [], {}
        for emoji, sector, tickers in sectors:
            for t in tickers:
                if t not in ticker_to_sector:
                    all_tickers.append(t)
                    ticker_to_sector[t] = f"{emoji} {sector}"

        scored = score_tickers_quick(all_tickers)
        if not scored:
            return ""

        buy_items = [item for item in scored if item["signal"] == "🟢BUY"]
        if not buy_items:
            return ""

        lines = [f"\n―――――― 🟢 BUY 추천 종목 ――――――"]
        for item in buy_items:
            t      = item["ticker"]
            raw    = ticker_to_sector.get(t, "")
            # 섹터명 단축: 이모지 + 첫 단어만 (괄호·가운뎃점 이후 제거)
            parts  = raw.split(" ", 1)
            emoji_ = parts[0] if len(parts) > 1 else ""
            name_  = parts[1] if len(parts) > 1 else raw
            name_  = name_.split("(")[0].split("·")[0].strip()
            sec    = f"{emoji_}{name_}"
            m3     = f"{item['mom3m']*100:+.1f}%" if item["mom3m"] is not None else "N/A"
            lines.append(
                f"🟢 {t} [{sec}] 점수:{item['score']:.2f} 3M:{m3} RSI:{item['rsi']:.0f}"
            )
        return "\n".join(lines)
    except Exception as e:
        print(f"[quant_picks] 실패: {e}")
        return ""


async def cmd_econ(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """미국 경기 국면 분석 — /econ 또는 '경기지표 알려줘'"""
    if not is_authorized(update):
        return
    msg = await update.message.reply_text("📊 경기지표 수집 중... (30초 내외 소요)")
    try:
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, get_econ_cycle)

        # 캐시·국면 상태 업데이트
        save_econ_cache(result)
        last_phase    = load_last_phase()
        current_phase = result["phase"]
        save_last_phase(current_phase)

        report = format_econ_report(result)

        # 국면 전환 시 최상단에 경보 배너 추가
        if last_phase and last_phase != current_phase:
            report = (
                f"🚨 *경기 국면 전환 감지!*\n"
                f"*{last_phase}* → *{current_phase}*\n\n"
                + report
            )

        # 퀀트 계산 중임을 먼저 표시
        await msg.edit_text(report + "\n\n⏳ BUY 종목 퀀트 점수 계산 중...", parse_mode="Markdown")

        picks = await loop.run_in_executor(None, lambda: _get_phase_quant_picks(current_phase))
        full  = report + (picks if picks else "")
        await msg.edit_text(full, parse_mode="Markdown")

    except Exception as e:
        await msg.edit_text(f"❌ 경기지표 조회 실패: {e}")


async def _run_econ_and_notify(context, header: str) -> None:
    """경기 분석 실행 → 캐시 갱신 → 전체 사용자에게 전송 (공통 로직)."""
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, get_econ_cycle)
    save_econ_cache(result)
    last_phase    = load_last_phase()
    current_phase = result["phase"]
    save_last_phase(current_phase)

    report = format_econ_report(result)
    if last_phase and last_phase != current_phase:
        report = (
            f"🚨 *경기 국면 전환 감지!*\n"
            f"*{last_phase}* → *{current_phase}*\n\n"
            + report
        )

    picks = await loop.run_in_executor(None, lambda: _get_phase_quant_picks(current_phase))
    full  = f"{header}\n\n{report}" + (picks if picks else "")
    for cid in AUTHORIZED_CHATS:
        try:
            for chunk in smart_split(full):
                await context.bot.send_message(chat_id=int(cid), text=chunk, parse_mode="Markdown")
        except Exception:
            pass



async def job_econ_monthly(context) -> None:
    """매월 28일 오전 8시 KST 자동 경기 국면 브리핑."""
    if datetime.now(KST).day != 28:
        return
    try:
        await _run_econ_and_notify(context, "📅 *월말 경기 국면 자동 브리핑*")
    except Exception as e:
        for cid in AUTHORIZED_CHATS:
            try:
                await context.bot.send_message(chat_id=int(cid), text=f"⚠️ 월말 경기 브리핑 실패: {e}")
            except Exception:
                pass


async def job_econ_phase_check(context) -> None:
    """매주 일요일 오전 9시 KST 경기 국면 점검 — 전환 시 즉시 알림."""
    try:
        await _run_econ_and_notify(context, "🔍 *주간 경기 국면 점검*")
    except Exception as e:
        print(f"[econ_phase_check] 실패: {e}")


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

    def _process_image() -> tuple[str, str]:
        """블로킹 네트워크 호출(매크로/리서치/Claude) 묶음 — executor에서 실행. (img_type, reply) 반환."""
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
            }],
            timeout=60,
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
            research_parts = []
            for s in stocks[:6]:
                data = research_stock(s)
                if data and "리서치 데이터 없음" not in data:
                    flow = get_investor_flow(s)
                    if flow:
                        data += f"\n{flow}"
                    research_parts.append(data)
            research_text = "\n\n".join(research_parts) if research_parts else "수집된 리서치 데이터 없음"

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
                }],
                timeout=90,
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

            # 수급 데이터
            flow_info = get_investor_flow(chart_stock) if chart_stock else ""

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
{flow_info}

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
                }],
                timeout=90,
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
                }],
                timeout=60,
            )
            reply = f"🔍 *이미지 분석*\n\n{analysis.content[0].text}"

        return img_type, reply

    try:
        loop = asyncio.get_event_loop()
        img_type, reply = await asyncio.wait_for(
            loop.run_in_executor(None, _process_image), timeout=150,
        )
    except asyncio.TimeoutError:
        tmp_path.unlink(missing_ok=True)
        await msg.edit_text("❌ 이미지 분석 시간 초과 (150초). 잠시 후 다시 시도해주세요.")
        return
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        await msg.edit_text(f"❌ 이미지 분석 실패: {e}")
        return

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

# 한국어 종목명 → 미국 티커 매핑 (실시간 가격 오류 방지)
_KR_TO_US: dict[str, str] = {
    "알파벳": "GOOGL", "구글": "GOOGL",
    "애플": "AAPL",
    "마이크로소프트": "MSFT", "MS": "MSFT",
    "엔비디아": "NVDA",
    "아마존": "AMZN",
    "메타": "META",
    "테슬라": "TSLA",
    "AMD": "AMD",
    "인텔": "INTC",
    "넷플릭스": "NFLX",
    "팔란티어": "PLTR",
    "브로드컴": "AVGO",
}

def _detect_stocks_in_text(text: str) -> list[str]:
    """질문 텍스트에서 종목명 추출 (한국 + 미국)"""
    import re as _re
    from stock_research import KR_STOCK_MAP

    found = []
    text_upper = text.upper()   # "sk하이닉스" 같은 소문자 입력도 매칭되도록 대소문자 무시
    # 한국 주식: 사전 기반
    for name in KR_STOCK_MAP:
        if name.upper() in text_upper and name not in found:
            found.append(name)
    # 짧은 종목명이 다른 매칭 종목명의 부분 문자열이면 제거
    # 예) "SK하이닉스 알려줘" → "SK"(SK㈜, 별개 종목)가 "SK하이닉스"의 부분으로 함께 매칭되는 것 방지
    found = [n for n in found if not any(n != other and n in other for other in found)]
    # 미국 주식: 한국어 이름 → 티커 변환 (실시간 가격 보장)
    for kr_name, ticker in _KR_TO_US.items():
        if kr_name in text and ticker not in found:
            found.append(ticker)
    # 미국 주식: 대문자 티커 직접 입력
    for ticker in _re.findall(r'\b[A-Z]{2,5}\b', text):
        if ticker in _US_TICKERS and ticker not in found:
            found.append(ticker)
    return found[:4]   # 최대 4개


_KR_QUANT_TRIGGERS = [
    "한국 퀀트", "한국퀀트", "코스피 퀀트", "코스피퀀트",
    "국내 퀀트", "국내퀀트", "퀀트 신호", "퀀트신호",
    "퀀트 알려줘", "퀀트알려줘",
]
_US_QUANT_TRIGGERS = [
    "미국 퀀트", "미국퀀트", "나스닥 퀀트", "나스닥퀀트",
    "us 퀀트", "us퀀트", "미국 주식 퀀트", "미국주식퀀트",
]
_ECON_TRIGGERS = [
    "경기지표", "경기 지표", "경기국면", "경기 국면",
    "경기분석", "경기 분석", "경기사이클", "경기 사이클",
    "경기 알려줘", "경기알려줘", "경제지표", "경제 지표",
]
_FEAR_GREED_TRIGGERS = [
    "공포탐욕", "공포 탐욕", "탐욕공포", "탐욕 공포",
    "공포지수", "공포 지수", "탐욕지수", "탐욕 지수",
    "fear greed", "fear & greed", "fear and greed", "fng", "f&g",
]

# ── CNN 공포·탐욕 지수 (실측 조회) ──────────────────────────────

_CNN_FG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Origin": "https://edition.cnn.com",
}

_FG_LABEL = {
    "extreme fear": ("😱", "극도의 공포"),
    "fear":         ("😨", "공포"),
    "neutral":      ("😐", "중립"),
    "greed":        ("😄", "탐욕"),
    "extreme greed": ("🤑", "극도의 탐욕"),
}

# CNN 세부 구성지표 (표시할 7개, key → 한글명)
_FG_COMPONENTS = [
    ("market_momentum_sp500", "시장 모멘텀 (S&P500)"),
    ("stock_price_strength",  "주가 강도 (신고가/신저가)"),
    ("stock_price_breadth",   "주가 폭 (등락 거래량)"),
    ("put_call_options",      "풋/콜 옵션"),
    ("market_volatility_vix", "변동성 (VIX)"),
    ("junk_bond_demand",      "정크본드 수요"),
    ("safe_haven_demand",     "안전자산 수요"),
]


def _fg_desc(rating: str) -> str:
    return _FG_LABEL.get((rating or "").lower().strip(), ("📊", rating or "?"))[1]


def fetch_fear_greed_detail() -> str:
    """CNN Fear & Greed Index 실측값 상세 요약. 실패 시 빈 문자열."""
    try:
        res = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/",
            timeout=8, headers=_CNN_FG_HEADERS,
        )
        if res.status_code != 200:
            return ""
        data = res.json()
        fg    = data["fear_and_greed"]
        score = fg["score"]
        emoji, label = _FG_LABEL.get(
            str(fg.get("rating", "")).lower().strip(), ("📊", fg.get("rating", "?"))
        )

        lines = [
            f"{emoji} CNN 공포·탐욕 지수 (Fear & Greed Index)",
            "",
            f"현재: {score:.0f}점 — {label}",
            "",
            "📅 추이",
            f"· 전일 종가: {fg['previous_close']:.0f}점",
            f"· 1주 전: {fg['previous_1_week']:.0f}점",
            f"· 1달 전: {fg['previous_1_month']:.0f}점",
            f"· 1년 전: {fg['previous_1_year']:.0f}점",
            "",
            "🧭 세부 구성지표",
        ]
        for key, kname in _FG_COMPONENTS:
            comp = data.get(key)
            if isinstance(comp, dict) and comp.get("score") is not None:
                lines.append(f"· {kname}: {comp['score']:.0f}점 ({_fg_desc(comp.get('rating'))})")

        lines += [
            "",
            "기준: 0(극단적 공포) ~ 100(극단적 탐욕) · 출처 CNN",
        ]
        return "\n".join(lines)
    except Exception:
        return ""


async def cmd_feargreed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    msg = await update.message.reply_text("💭 공포·탐욕 지수 조회 중...")
    loop = asyncio.get_event_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(None, fetch_fear_greed_detail), timeout=15
        )
    except asyncio.TimeoutError:
        text = ""
    if text:
        await msg.edit_text(text)
    else:
        await msg.edit_text(
            "❌ CNN 공포·탐욕 지수를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.\n"
            "실시간 확인: cnn.com/markets/fear-and-greed"
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    question = update.message.text.strip()
    chat_id  = update.effective_chat.id

    # ── 공포·탐욕 지수 감지 (실측값 직접 회신, AI 추정 금지) ──
    if any(t in question.lower() for t in _FEAR_GREED_TRIGGERS):
        await cmd_feargreed(update, context)
        return

    # ── 경기 국면 감지 ──
    if any(t in question for t in _ECON_TRIGGERS):
        await cmd_econ(update, context)
        return

    # ── 퀀트 대화체 감지 ──
    q_lower = question.lower()
    if any(t in q_lower for t in _US_QUANT_TRIGGERS):
        await cmd_quant_us(update, context)
        return
    if any(t in q_lower for t in _KR_QUANT_TRIGGERS):
        await cmd_quant(update, context)
        return

    # ── 가격 알림 취소 자연어 감지 ──
    # 예) "삼성전자 알림 취소해줘", "AAPL 알림 꺼줘"
    if "알림" in question and any(w in question for w in ["취소", "삭제", "꺼줘", "지워줘"]):
        stocks_to_cancel = _detect_stocks_in_text(question)
        if stocks_to_cancel:
            cid_str     = str(update.effective_chat.id)
            name        = stocks_to_cancel[0]
            alerts      = _load_alerts()
            user_alerts = alerts.get(cid_str, [])
            before      = len(user_alerts)
            alerts[cid_str] = [a for a in user_alerts if name not in a["name"]]
            _save_alerts(alerts)
            removed = before - len(alerts[cid_str])
            reply   = f"✅ '{name}' 알림 {removed}개 취소 완료." if removed else f"❌ '{name}' 등록된 알림이 없습니다."
            await update.message.reply_text(reply)
            return

    # ── 가격 알림 자연어 감지 ──
    # 예) "삼성전자 7만원 넘으면 알려줘", "AAPL 200달러 되면 알림"
    if any(kw in question for kw in _ALERT_NATURAL_WORDS):
        stocks_for_alert = _detect_stocks_in_text(question)
        price_match = _PRICE_RE.search(question)
        if stocks_for_alert and price_match:
            target = _parse_korean_price(price_match.group(0))
            if target and target > 0:
                direction = None
                if any(w in question for w in _ABOVE_WORDS):
                    direction = "above"
                elif any(w in question for w in _BELOW_WORDS):
                    direction = "below"
                await _do_register_alert(update, stocks_for_alert[0], target, direction)
                return

    msg = await update.message.reply_text("💭 분석 중...")

    detected = _detect_stocks_in_text(question)
    if detected:
        await msg.edit_text(f"🔍 {', '.join(detected)} 데이터 수집 중...")

    history = _history(chat_id)

    def _build_reply() -> str:
        """블로킹 네트워크 호출(매크로/뉴스/종목 조회/Claude) 묶음 — executor에서 실행."""
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
        research_text = ""
        price_summary = ""   # 현재가 요약 (시스템 프롬프트 상단에 명시)
        if detected:
            parts = []
            for stock in detected:
                data = research_stock(stock)
                if data and "데이터 없음" not in data:
                    flow = get_investor_flow(stock)
                    if flow:
                        data += f"\n{flow}"
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
        price_block = (
            chr(10) + "[현재가 (이 수치를 기준으로 모든 원 단위 계산)]" + chr(10) + price_summary
            if price_summary else
            chr(10) + "[알림] 실시간 가격 데이터 조회에 실패했습니다. 가격을 추측해서 답변하지 말고, "
            "가격이 필요한 질문에는 '현재가 조회에 실패했습니다. 잠시 후 다시 시도해주세요'라고 명확히 안내하세요." + chr(10)
            if detected else ""
        )

        system_prompt = f"""당신은 한국 주식/투자 전문 비서입니다.
이전 대화 맥락을 기억하고 이어서 답변하세요.
{price_block}
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
- 위에 현재가가 제공되지 않았다면 절대 가격을 추측하지 말 것 (위 [알림] 지침 따름)
- 절대 사용자에게 현재가를 물어보지 말 것. 현재가가 제공된 경우 그것을 사용
- 이전 대화에서 언급된 종목/주제가 있으면 자연스럽게 연결
- 투자 결정은 본인 책임임을 마지막 한 줄에 언급
- 텔레그램 일반 텍스트 메시지 규칙: 마크다운 헤더(#, ##, ###), 볼드(**), 기울임(*), 수평선(---), 표(|) 절대 사용 금지. 이모지 + 일반 텍스트로만 구성
- 한국어, 800자 내외 (길면 자동으로 여러 메시지로 분할됨)"""

        messages = history + [{"role": "user", "content": question}]
        return claude().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1800,
            system=system_prompt,
            messages=messages,
            timeout=60,
        ).content[0].text

    try:
        loop  = asyncio.get_event_loop()
        reply = await asyncio.wait_for(loop.run_in_executor(None, _build_reply), timeout=90)
    except asyncio.TimeoutError:
        await msg.edit_text("❌ 응답 생성 시간 초과 (90초). 잠시 후 다시 시도해주세요.")
        return
    except Exception as e:
        await msg.edit_text(f"❌ 오류: {e}")
        return

    # ── 히스토리 저장 ──
    _save(chat_id, "user",      question)
    _save(chat_id, "assistant", reply)

    await safe_send(msg, f"💬 {reply}", edit=True, user_msg=update.message, context=context)


# ── /quant_us (미국 퀀트 신호) ───────────────────────────────────

async def cmd_quant_us(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    top_n = 15
    for arg in (context.args or []):
        if arg.isdigit():
            top_n = int(arg)

    await update.message.reply_text(
        f"⏳ 미국 퀀트 분석 중... (1~2분 소요)\n"
        f"S&P500 상위 + Nasdaq100 팩터 스코어링 (상위 {top_n}종목)"
    )
    try:
        from quant_us import run_us_quant
        requester = str(update.effective_chat.id)
        loop = asyncio.get_event_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: run_us_quant(top_n, send_to=[requester])),
            timeout=300,
        )
        await update.message.reply_text("✅ 미국 퀀트 분석 완료!")
    except asyncio.TimeoutError:
        await update.message.reply_text("❌ 시간 초과 (5분). 잠시 후 다시 시도해주세요.")
    except Exception as e:
        await update.message.reply_text(f"❌ 오류: {e}")


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


# ── /debugprice (가격 소스 진단, 텔레그램으로 직접 결과 회신) ──

async def cmd_debugprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    code = (context.args[0] if context.args else "000660").strip()

    from stock_research import _get_kr_price_naver, _get_kr_price_naver_mobile, _get_kr_price_pykrx

    lines = [f"🔍 가격 소스 진단: {code}"]

    try:
        naver = _get_kr_price_naver(code)
        lines.append(f"naver_polling: {naver or 'EMPTY'}")
    except Exception as e:
        lines.append(f"naver_polling: 예외 — {e}")

    try:
        mobile = _get_kr_price_naver_mobile(code)
        lines.append(f"naver_mobile: {mobile or 'EMPTY'}")
    except Exception as e:
        lines.append(f"naver_mobile: 예외 — {e}")

    try:
        pykrx_price = _get_kr_price_pykrx(code)
        lines.append(f"pykrx: {pykrx_price}")
    except Exception as e:
        lines.append(f"pykrx: 예외 — {e}")

    try:
        yf_t = yf.Ticker(f"{code}.KS")
        yf_price = yf_t.fast_info.last_price
        lines.append(f"yfinance(.KS): {yf_price}")
    except Exception as e:
        lines.append(f"yfinance(.KS): 예외 — {e}")

    await update.message.reply_text("\n".join(lines))


# ── 봇 실행 ───────────────────────────────────────────────────

from aiohttp import web as _web

def _build_app() -> Application:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_start))
    app.add_handler(CommandHandler("market",    cmd_market))
    app.add_handler(CommandHandler("brief",     cmd_brief))
    app.add_handler(CommandHandler("quant",     cmd_quant))
    app.add_handler(CommandHandler("quant_us",  cmd_quant_us))
    app.add_handler(CommandHandler("alert",     cmd_alert))
    app.add_handler(CommandHandler("econ",      cmd_econ))
    app.add_handler(CommandHandler("feargreed", cmd_feargreed))
    app.add_handler(CommandHandler("debugprice", cmd_debugprice))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.job_queue.run_daily(
        job_morning_brief,
        time=dtime(hour=7, minute=50, second=0, tzinfo=KST),
        days=(0, 1, 2, 3, 4),   # 월~금만 (0=월요일) — 미장 휴장 주말 제외
        name="morning_brief_daily",
    )
    app.job_queue.run_repeating(
        job_check_alerts,
        interval=300,   # 5분마다
        first=60,
        name="price_alert_check",
    )
    app.job_queue.run_daily(
        job_econ_monthly,
        time=dtime(hour=8, minute=0, second=0, tzinfo=KST),
        name="econ_monthly_brief",
    )
    app.job_queue.run_daily(
        job_econ_phase_check,
        time=dtime(hour=9, minute=0, second=0, tzinfo=KST),
        days=(6,),   # 일요일 (0=월, 6=일)
        name="econ_phase_weekly",
    )
    return app


async def _async_main():
    port       = int(os.getenv("PORT", 10000))
    render_url = (os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/") or
                  "https://morning-brief-bot-cvfd.onrender.com")
    wh_path    = f"/{TOKEN}"

    ptb = _build_app()

    # aiohttp 서버: GET / → 헬스체크(200), POST /{TOKEN} → webhook
    async def on_health(req):
        return _web.Response(text="OK")

    async def on_webhook(req):
        try:
            data   = await req.json()
            update = Update.de_json(data, ptb.bot)
            await ptb.update_queue.put(update)
        except Exception:
            pass
        return _web.Response(text="OK")

    web_app = _web.Application()
    web_app.router.add_get("/",      on_health)
    web_app.router.add_post(wh_path, on_webhook)

    runner = _web.AppRunner(web_app)
    await runner.setup()
    await _web.TCPSite(runner, "0.0.0.0", port).start()

    await ptb.initialize()
    await ptb.bot.set_webhook(
        url=f"{render_url}{wh_path}",
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
    await ptb.start()

    now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_kst} KST] 봇 시작! (aiohttp webhook port={port})", flush=True)

    try:
        await asyncio.Event().wait()
    finally:
        await ptb.stop()
        await ptb.shutdown()
        await runner.cleanup()


def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
