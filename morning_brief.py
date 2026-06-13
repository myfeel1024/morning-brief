"""
====================================================
  🇺🇸 코스피 모닝 브리핑 자동화 스크립트
====================================================
기능:
  1. 간밤 미국 증시 (S&P500, Nasdaq, Dow, 섹터 ETF)
  2. 매크로 지표 (금리, 달러, 유가, VIX)
  3. 한국 야간선물 (코스피200 선물, 달러/원 선물)
  4. 주요 뉴스 헤드라인 (NewsAPI)
  5. Claude AI가 생성하는 코스피 투자 전략
  6. 내 보유 종목 분석 (사진 업로드 시 자동 실행)

필요한 API 키 (아래 설정 섹션에 입력):
  - Telegram Bot Token + Chat ID
  - NewsAPI  : https://newsapi.org (무료)
  - Anthropic: https://console.anthropic.com (유료, 월 $1~2)

설치:
  pip install yfinance requests anthropic pillow
====================================================
"""

import re
import yfinance as yf
import requests
import anthropic
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
# .env 파일을 스크립트 위치 기준으로 직접 읽어서 환경변수 주입
def _load_env_file():
    env_path = Path(__file__).resolve().parent / '.env'
    if not env_path.exists():
        return
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ[key] = value  # 빈 환경변수도 덮어씀

_load_env_file()

# ============================================================
#  ★ 설정 — .env 파일에 API 키를 입력하세요
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
NEWS_API_KEY       = os.getenv("NEWS_API_KEY", "")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")

# 내 보유 종목 (사진 없이 수동으로 추가할 때 사용)
# 예: MY_PORTFOLIO = ["삼성전자", "SK하이닉스", "현대차", "NAVER"]
MY_PORTFOLIO = []

# ============================================================


# ── 1. 미국 지수 & 매크로 지표 수집 ──────────────────────────

INDICES = {
    "S&P 500"    : "^GSPC",
    "Nasdaq"     : "^IXIC",
    "Dow Jones"  : "^DJI",
    "VIX 공포지수" : "^VIX",
    "미 10년물 금리": "^TNX",
    "달러 인덱스"  : "DX-Y.NYB",
    "WTI 유가"   : "CL=F",
    "금"         : "GC=F",
}

SECTORS = {
    "기술 (XLK)"  : "XLK",
    "금융 (XLF)"  : "XLF",
    "헬스케어(XLV)": "XLV",
    "에너지 (XLE)": "XLE",
    "소비재 (XLY)": "XLY",
    "필수소비(XLP)": "XLP",
    "산업재 (XLI)": "XLI",
    "반도체(SOXX)": "SOXX",
}

# 한국 관련 야간 지표 (미국 시간대에 거래되는 한국 연계 상품)
# - EWY: 한국 ETF (미국 상장, 코스피 야간 흐름 가늠용 핵심 지표)
# - 코스피200 선물(^KS200F)은 yfinance 미지원이라 EWY로 대체
# - USDKRW=X: 달러/원 환율 (야간 변동)
KOREA_NIGHT = {
    "한국 ETF (EWY)" : "EWY",
    "달러/원 환율"    : "USDKRW=X",
}


def fetch_price(ticker_symbol):
    """단일 티커의 현재가·전일종가·등락률 반환"""
    try:
        info  = yf.Ticker(ticker_symbol).fast_info
        price = info.last_price
        prev  = info.previous_close
        if not price or not prev:
            return None
        pct   = (price - prev) / prev * 100
        return {"price": price, "prev": prev, "pct": pct}
    except Exception:
        return None


def build_market_summary():
    """지수·매크로·섹터 데이터를 텍스트 블록으로 조합"""
    lines = []

    lines.append("📊 *미국 지수 & 매크로*")
    for name, sym in INDICES.items():
        d = fetch_price(sym)
        if d:
            arrow = "🔺" if d["pct"] >= 0 else "🔻"
            sign  = "+" if d["pct"] >= 0 else ""
            lines.append(f"`{name:<14}{d['price']:>10,.2f}  {arrow}{sign}{d['pct']:.2f}%`")
        else:
            lines.append(f"`{name:<14}  데이터 없음`")

    lines.append("")
    lines.append("🏭 *섹터 ETF 등락*")
    for name, sym in SECTORS.items():
        d = fetch_price(sym)
        if d:
            arrow = "🔺" if d["pct"] >= 0 else "🔻"
            sign  = "+" if d["pct"] >= 0 else ""
            lines.append(f"`{name:<14}{d['price']:>8,.2f}  {arrow}{sign}{d['pct']:.2f}%`")
        else:
            lines.append(f"`{name:<14}  데이터 없음`")

    lines.append("")
    lines.append("🇰🇷 *한국 야간 흐름*")
    for name, sym in KOREA_NIGHT.items():
        d = fetch_price(sym)
        if d:
            arrow = "🔺" if d["pct"] >= 0 else "🔻"
            sign  = "+" if d["pct"] >= 0 else ""
            lines.append(f"`{name:<14}{d['price']:>8,.2f}  {arrow}{sign}{d['pct']:.2f}%`")
        else:
            lines.append(f"`{name:<14}  데이터 없음`")

    return "\n".join(lines)


def get_market_data_for_ai():
    """Claude 분석용 딕셔너리 형태 데이터"""
    data = {}
    for name, sym in {**INDICES, **SECTORS, **KOREA_NIGHT}.items():
        d = fetch_price(sym)
        if d:
            data[name] = d
    return data


# ── 2. 뉴스 수집 (NewsAPI) ────────────────────────────────────

def fetch_news(query: str = None, max_articles: int = 9):
    """NewsAPI에서 경제·실적·금리·물가·지정학 뉴스 수집 (금융 전문지만)"""
    if not NEWS_API_KEY:
        return ["NewsAPI 키 미설정 — .env 파일에 NEWS_API_KEY를 입력하세요."]

    # 경제·시장 전반 — 좁은 키워드 대신 도메인 내 기사 전반 수집
    if query is None:
        query = (
            "stock OR market OR economy OR stocks OR finance OR investment "
            "OR Fed OR inflation OR earnings OR tariff OR oil OR dollar"
        )

    # 경제·금융 전문지만 허용 (일반 뉴스·정치 매체 제외)
    trusted_domains = (
        "reuters.com,bloomberg.com,wsj.com,ft.com,"
        "cnbc.com,marketwatch.com,economist.com,"
        "barrons.com,seekingalpha.com,apnews.com,"
        "investing.com,finance.yahoo.com"
    )

    # 무의미한 헤드라인 필터 키워드
    skip_keywords = [
        "morning briefing", "daily briefing", "news briefing",
        "what to know", "what's happening", "here's what",
        "today in", "this week in", "roundup",
    ]

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    url = (
        "https://newsapi.org/v2/everything"
        f"?q={requests.utils.quote(query)}"
        f"&domains={trusted_domains}"
        f"&from={yesterday}"
        "&language=en"
        "&sortBy=relevancy"
        f"&pageSize={max_articles}"
        f"&apiKey={NEWS_API_KEY}"
    )
    try:
        res  = requests.get(url, timeout=10)
        data = res.json()
        if data.get("status") != "ok":
            return [f"뉴스 API 오류: {data.get('message', '알 수 없음')}"]
        articles = data.get("articles", [])
        results = []
        for a in articles:
            title = a.get("title", "")
            if not title:
                continue
            # 무의미한 헤드라인 제외
            if any(kw in title.lower() for kw in skip_keywords):
                continue
            source = a.get("source", {}).get("name", "")
            results.append(f"{title} ({source})")
        return results if results else [f"관련 뉴스를 찾을 수 없습니다."]
    except Exception as e:
        return [f"뉴스 수집 실패: {e}"]


def translate_headlines_to_korean(headlines: list) -> list:
    """Claude API로 뉴스 헤드라인을 한국어로 번역"""
    if not ANTHROPIC_API_KEY or not headlines:
        return headlines
    # 에러/안내 메시지는 번역 건너뜀
    if len(headlines) == 1 and any(k in headlines[0] for k in ("미설정", "실패", "오류")):
        return headlines

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    numbered = "\n".join(f"{i+1}. {h}" for i, h in enumerate(headlines))

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": (
                    "아래 영어 뉴스 헤드라인을 자연스러운 한국어로 번역하세요.\n"
                    "규칙: 번호 유지, 괄호 안 출처명 유지, 번역문만 출력(설명 없이).\n\n"
                    f"{numbered}"
                )
            }]
        )
        lines = [
            re.sub(r'^\d+\.\s*', '', l.strip())
            for l in msg.content[0].text.strip().split('\n')
            if l.strip()
        ]
        return lines if len(lines) == len(headlines) else headlines
    except Exception:
        return headlines


# ── 3. Claude AI 분석 ─────────────────────────────────────────

def analyze_with_claude(market_data: dict, news_list: list, portfolio: list = None):
    """Claude API로 시황 분석 및 코스피 전략 생성"""
    if not ANTHROPIC_API_KEY:
        return "⚠️ Anthropic API 키 미설정 — .env 파일에 ANTHROPIC_API_KEY를 입력하세요."

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # 지표 요약 텍스트
    market_text = "\n".join(
        f"  {k}: {v['price']:.2f} ({'+' if v['pct']>=0 else ''}{v['pct']:.2f}%)"
        for k, v in market_data.items()
    )
    news_text = "\n".join(news_list[:6])

    portfolio_section = ""
    if portfolio:
        portfolio_section = f"""
[보유 종목]
{', '.join(portfolio)}
위 종목들의 미국 시장 동향과의 연관성, 오늘 주목할 점도 간략히 언급해 주세요.
"""

    prompt = f"""당신은 한국 주식시장 전문 애널리스트입니다.
아래 간밤 미국 시장 데이터와 주요 뉴스를 바탕으로, 오늘 한국 코스피 투자자에게 도움이 되는 모닝 브리핑을 작성하세요.

[간밤 미국 시장 데이터]
{market_text}

[주요 뉴스 헤드라인]
{news_text}
{portfolio_section}

아래 형식으로 한국어로 작성하세요 (텔레그램 전송용, 총 500자 내외):

① 간밤 한 줄 요약
② 핵심 매크로 포인트
   - 미 10년물 국채금리 수준과 방향 (반드시 포함)
   - 달러/원 환율 영향
   - 유가·원자재 이슈 (해당 시)
③ 주요 기업 실적 (어닝 서프라이즈/쇼크 있으면 반드시 언급)
④ 주목 섹터 (코스피 관점에서 오늘 강세/약세 예상 섹터)
⑤ 오늘의 코스피 전략 한 줄
⑥ 리스크 요인 (있다면)
"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text
    except Exception as e:
        return f"AI 분석 실패: {e}"


# ── 4. 보유 종목 이미지 분석 ──────────────────────────────────

def extract_portfolio_from_image(image_path: str):
    """
    주식 앱 스크린샷에서 종목명을 추출하고 투자 조언 생성
    사용법: python morning_brief.py /path/to/portfolio_screenshot.png
    """
    if not ANTHROPIC_API_KEY:
        return None, "Anthropic API 키 미설정 — .env 파일에 ANTHROPIC_API_KEY를 입력하세요."

    path = Path(image_path)
    if not path.exists():
        return None, f"파일 없음: {image_path}"

    # 이미지를 base64로 인코딩
    suffix   = path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png",  ".webp": "image/webp"}
    media_type = mime_map.get(suffix, "image/jpeg")

    with open(path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Step 1: 종목 추출
    try:
        extract_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        }
                    },
                    {
                        "type": "text",
                        "text": (
                            "이 이미지는 주식 앱의 보유 종목 화면입니다. "
                            "종목명(한국어 또는 영어 티커)만 추출해서 "
                            "JSON 배열 형태로 반환하세요. "
                            "예: [\"삼성전자\", \"SK하이닉스\", \"NAVER\"]\n"
                            "종목명만 포함하고 다른 텍스트는 포함하지 마세요."
                        )
                    }
                ]
            }]
        )

        raw      = extract_msg.content[0].text.strip()
        # JSON 파싱 시도
        clean    = raw.replace("```json", "").replace("```", "").strip()
        stocks   = json.loads(clean)
        if not isinstance(stocks, list):
            stocks = []

    except Exception as e:
        return None, f"종목 추출 실패: {e}"

    if not stocks:
        return None, "이미지에서 종목을 찾을 수 없었습니다."

    # Step 2: 추출된 종목으로 투자 분석
    market_data   = get_market_data_for_ai()
    market_text   = "\n".join(
        f"  {k}: {v['price']:.2f} ({'+' if v['pct']>=0 else ''}{v['pct']:.2f}%)"
        for k, v in market_data.items()
    )
    news_list = fetch_news(query=" OR ".join(stocks[:3]) + " stock Korea")
    news_text = "\n".join(news_list[:4])

    analysis_prompt = f"""당신은 한국 주식 전문 애널리스트입니다.

[보유 종목]
{', '.join(stocks)}

[간밤 미국 시장]
{market_text}

[관련 뉴스]
{news_text}

위 보유 종목 각각에 대해 오늘의 투자 포인트를 간략히 정리해 주세요.
형식: 종목명 — 오늘 주목할 점 (1~2줄)
마지막에 전체 포트폴리오 관점에서 오늘 전략 한 줄 추가.
"""

    try:
        analysis_msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": analysis_prompt}]
        )
        analysis = analysis_msg.content[0].text
        return stocks, analysis
    except Exception as e:
        return stocks, f"포트폴리오 분석 실패: {e}"


# ── 5. CNN 공포탐욕지수 ──────────────────────────────────────

def get_fear_greed() -> str:
    """CNN Fear & Greed Index 한 줄 요약. 실패 시 빈 문자열."""
    try:
        res  = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/",
            timeout=6,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data  = res.json()
        score = data["fear_and_greed"]["score"]
        rating = data["fear_and_greed"]["rating"]
        emoji = {
            "Extreme Fear": "😱", "Fear": "😨",
            "Neutral": "😐", "Greed": "😄", "Extreme Greed": "🤑",
        }.get(rating, "📊")
        label = {
            "Extreme Fear": "극도의 공포", "Fear": "공포",
            "Neutral": "중립", "Greed": "탐욕", "Extreme Greed": "극도의 탐욕",
        }.get(rating, rating)
        return f"{emoji} CNN 공포탐욕지수: {score:.0f}점 — {label}"
    except Exception:
        return ""


# ── 6. 텔레그램 전송 ──────────────────────────────────────────

def send_telegram(text: str, send_to: list[str] | None = None):
    """텔레그램 봇으로 메시지 전송 (4096자 초과 시 분할).
    send_to 지정 시 해당 chat_id 목록에만 전송, 없으면 전체 브로드캐스트.
    """
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️  텔레그램 설정 없음 — 콘솔에만 출력합니다.\n")
        print(text)
        return

    url      = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_len  = 4000
    chunks   = [text[i:i+max_len] for i in range(0, len(text), max_len)]
    chat_ids = send_to if send_to else [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]

    for cid in chat_ids:
        for chunk in chunks:
            payload = {
                "chat_id"   : cid,
                "text"      : chunk,
                "parse_mode": "Markdown",
            }
            try:
                res = requests.post(url, data=payload, timeout=10)
                if res.status_code != 200:
                    print(f"전송 오류 (chat_id={cid}): {res.text}")
            except Exception as e:
                print(f"전송 실패 (chat_id={cid}): {e}")


# ── 6. 메인 실행 ──────────────────────────────────────────────

def run_morning_brief(portfolio_image_path: str = None, send_to: list[str] | None = None):
    now = datetime.now().strftime("%Y년 %m월 %d일 %H:%M")
    print(f"[{now}] 브리핑 생성 시작...")

    # ─ 데이터 수집
    print("  → 시장 데이터 수집 중...")
    market_summary = build_market_summary()
    market_data    = get_market_data_for_ai()

    print("  → 뉴스 수집 및 번역 중...")
    news_list = fetch_news()                                  # 기본 쿼리(경제·금리·지정학)
    news_kr   = translate_headlines_to_korean(news_list)     # 한국어 번역
    news_block = "📰 *주요 뉴스*\n" + "\n".join(
        f"• {h}" for h in news_kr[:8]
    )

    # ─ 포트폴리오 이미지 처리
    portfolio      = list(MY_PORTFOLIO)  # 수동 목록 복사
    portfolio_block = ""

    if portfolio_image_path:
        print(f"  → 포트폴리오 이미지 분석 중: {portfolio_image_path}")
        extracted, analysis = extract_portfolio_from_image(portfolio_image_path)
        if extracted:
            portfolio += [s for s in extracted if s not in portfolio]
            portfolio_block = (
                "\n\n💼 *내 보유 종목 분석*\n"
                f"추출된 종목: {', '.join(extracted)}\n\n"
                f"{analysis}"
            )
        else:
            portfolio_block = f"\n\n💼 *포트폴리오 분석 실패*: {analysis}"

    # ─ AI 분석 (번역된 뉴스 사용)
    print("  → Claude AI 분석 중...")
    ai_analysis = analyze_with_claude(market_data, news_kr, portfolio or None)
    ai_block    = f"🤖 *AI 코스피 전략*\n{ai_analysis}"

    # ─ 공포탐욕지수
    fear_greed = get_fear_greed()

    # ─ 최종 메시지 조합
    separator = "\n" + "─" * 28 + "\n"
    message = (
        f"🇺🇸 *코스피 모닝 브리핑*\n`{now}`"
        + (f"\n{fear_greed}" if fear_greed else "")
        + separator
        + market_summary
        + separator
        + news_block
        + separator
        + ai_block
        + portfolio_block
        + separator
        + "_📌 본 브리핑은 투자 참고용이며 투자 결정의 책임은 본인에게 있습니다._"
    )

    # ─ 전송
    print("  → 텔레그램 전송 중...")
    send_telegram(message, send_to=send_to)
    print(f"[{now}] 완료!")


# ── 실행 진입점 ───────────────────────────────────────────────

if __name__ == "__main__":
    """
    일반 실행:
        python morning_brief.py

    포트폴리오 이미지 포함:
        python morning_brief.py /path/to/my_stocks.png

    PythonAnywhere 스케줄 명령어 (UTC 22:50 = KST 07:50):
        python /home/유저명/morning_brief.py
    """
    image_path = sys.argv[1] if len(sys.argv) > 1 else None
    run_morning_brief(portfolio_image_path=image_path)
