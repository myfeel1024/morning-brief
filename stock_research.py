"""
주식 리서치 모듈
- 한국 주식: 네이버 금융 증권사 리포트 + 목표주가 + Google News
- 미국 주식: yfinance 애널리스트 컨센서스 + NewsAPI
"""

import re
import requests
import urllib.parse
from xml.etree import ElementTree as ET
import yfinance as yf

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False


# ── 한국 주요 종목코드 매핑 ───────────────────────────────────

KR_STOCK_MAP = {
    "삼성전자": "005930", "SK하이닉스": "000660", "LG에너지솔루션": "373220",
    "삼성바이오로직스": "207940", "현대차": "005380", "기아": "000270",
    "NAVER": "035420", "네이버": "035420", "카카오": "035720",
    "LG화학": "051910", "삼성SDI": "006400", "현대모비스": "012330",
    "포스코홀딩스": "005490", "셀트리온": "068270", "KB금융": "105560",
    "신한지주": "055550", "하나금융지주": "086790", "우리금융지주": "316140",
    "카카오뱅크": "323410", "LG전자": "066570", "삼성물산": "028260",
    "SK이노베이션": "096770", "SK": "034730", "LG": "003550",
    "한국전력": "015760", "KT": "030200", "SK텔레콤": "017670",
    "한화에어로스페이스": "012450", "HD현대": "267250", "LG이노텍": "011070",
    "에코프로비엠": "247540", "에코프로": "086520", "포스코퓨처엠": "003670",
    "엔씨소프트": "036570", "크래프톤": "259960", "HMM": "011200",
    "삼성생명": "032830", "삼성전기": "009150", "하이브": "352820",
    "두산에너빌리티": "034020", "고려아연": "010130", "한국조선해양": "009540",
    "HD현대중공업": "329180", "현대로템": "064350", "한화오션": "042660",
}


def get_kr_stock_code(name: str) -> str:
    """종목명 → 네이버 금융 종목코드"""
    code = KR_STOCK_MAP.get(name, "")
    if code:
        return code
    # 네이버 자동완성 API 시도
    try:
        url = f"https://ac.finance.naver.com/api/ac?q={urllib.parse.quote(name)}&q_enc=UTF-8&target=stock"
        res = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        data = res.json()
        items = data.get("items", [[]])
        if items and items[0]:
            return items[0][0][0]
    except Exception:
        pass
    return ""


# ── Google News RSS (무료, API 불필요) ───────────────────────

def get_google_news(query: str, n: int = 6) -> list[str]:
    """Google News RSS로 뉴스 헤드라인 수집"""
    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(res.content)
        results = []
        for item in root.findall(".//item")[:n]:
            title_el = item.find("title")
            source_el = item.find("source")
            if title_el is not None and title_el.text:
                src = f" ({source_el.text})" if source_el is not None else ""
                results.append(f"{title_el.text}{src}")
        return results
    except Exception:
        return []


# ── 네이버 금융 증권사 리포트 스크래핑 ───────────────────────

def get_naver_analyst_reports(stock_code: str, n: int = 5) -> list[dict]:
    """네이버 금융 증권사 리포트 및 목표주가 수집"""
    if not BS4_OK or not stock_code:
        return []

    url = f"https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode={stock_code}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://finance.naver.com",
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")

        reports = []
        for row in soup.select("tr"):
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            texts = [c.get_text(strip=True) for c in cols]
            title_a = row.find("a")
            title = title_a.get_text(strip=True) if title_a else ""
            if not title or len(title) < 4:
                continue

            target, opinion, firm, date = "", "", "", ""
            for text in texts:
                if re.match(r"^[\d,]+원?$", text) and len(text) >= 5:
                    target = text if "원" in text else text + "원"
                elif text in ["매수", "중립", "매도", "BUY", "HOLD", "비중확대",
                               "시장수익률상회", "아웃퍼폼", "Outperform"]:
                    opinion = text
                elif re.match(r"\d{4}\.\d{2}\.\d{2}", text):
                    date = text
                elif (len(text) >= 2 and len(text) <= 15
                      and text != title
                      and not re.match(r"[\d,]+", text)
                      and not firm):
                    firm = text

            reports.append({
                "title": title[:60],
                "firm": firm,
                "target_price": target,
                "opinion": opinion,
                "date": date,
            })
            if len(reports) >= n:
                break
        return reports
    except Exception:
        return []


def get_naver_stock_news(stock_code: str, n: int = 5) -> list[str]:
    """네이버 금융 종목 최신 뉴스"""
    if not BS4_OK or not stock_code:
        return []
    url = f"https://finance.naver.com/item/news_news.naver?code={stock_code}&page=1"
    try:
        res = requests.get(url, headers={"User-Agent": "Mozilla/5.0",
                                         "Referer": "https://finance.naver.com"}, timeout=10)
        res.encoding = "utf-8"
        soup = BeautifulSoup(res.text, "html.parser")
        news = []
        for a in soup.select(".articleSubject a, .tltle a, td.title a"):
            t = a.get_text(strip=True)
            if t and len(t) > 5 and t not in news:
                news.append(t)
                if len(news) >= n:
                    break
        return news
    except Exception:
        return []


# ── 미국 주식 애널리스트 데이터 (yfinance) ────────────────────

def get_us_analyst_data(ticker: str) -> dict:
    """yfinance 미국 주식 애널리스트 컨센서스"""
    try:
        info = yf.Ticker(ticker).info
        result = {}
        rec_map = {1: "강력매수", 2: "매수", 3: "중립", 4: "매도", 5: "강력매도"}

        if info.get("currentPrice"):
            result["현재가"] = f"${info['currentPrice']:.2f}"
        if info.get("targetMeanPrice"):
            result["평균 목표주가"] = f"${info['targetMeanPrice']:.2f}"
        if info.get("targetHighPrice"):
            result["최고 목표주가"] = f"${info['targetHighPrice']:.2f}"
        if info.get("targetLowPrice"):
            result["최저 목표주가"] = f"${info['targetLowPrice']:.2f}"
        if info.get("recommendationMean"):
            v = round(info["recommendationMean"])
            result["투자의견"] = rec_map.get(v, str(info["recommendationMean"]))
        if info.get("numberOfAnalystOpinions"):
            result["참여 애널리스트"] = f"{info['numberOfAnalystOpinions']}명"
        if info.get("forwardPE"):
            result["Forward P/E"] = f"{info['forwardPE']:.1f}배"
        if info.get("trailingPE"):
            result["Trailing P/E"] = f"{info['trailingPE']:.1f}배"
        return result
    except Exception:
        return {}


# ── 종합 리서치 함수 ─────────────────────────────────────────

def research_stock(stock_name: str) -> str:
    """
    종목명으로 증권사 리포트, 목표주가, 뉴스를 수집하여 텍스트로 반환.
    한국 / 미국 주식 자동 판별.
    """
    parts = []
    is_us = bool(re.match(r"^[A-Z]{1,5}$", stock_name))

    if is_us:
        # ── 미국 주식 ──────────────────────────────────────
        analyst = get_us_analyst_data(stock_name)
        if analyst:
            lines = "\n".join(f"  {k}: {v}" for k, v in analyst.items())
            parts.append(f"[월가 애널리스트 컨센서스 - {stock_name}]\n{lines}")

        # 영어 뉴스
        eng_news = get_google_news(f"{stock_name} stock analyst target price outlook", 5)
        if eng_news:
            parts.append("[최신 뉴스]\n" + "\n".join(f"• {n}" for n in eng_news))

    else:
        # ── 한국 주식 ──────────────────────────────────────
        code = get_kr_stock_code(stock_name)

        # 1. 네이버 금융 증권사 리포트
        if code:
            reports = get_naver_analyst_reports(code, 5)
            if reports:
                lines = []
                for r in reports:
                    line = f"  [{r['firm'] or '?'}] {r['title']}"
                    if r["target_price"]:
                        line += f" | 목표주가 {r['target_price']}"
                    if r["opinion"]:
                        line += f" | {r['opinion']}"
                    if r["date"]:
                        line += f" ({r['date']})"
                    lines.append(line)
                parts.append(f"[증권사 리포트 - {stock_name}]\n" + "\n".join(lines))

        # 2. Google News - 목표주가 관련 헤드라인
        target_news = get_google_news(f"{stock_name} 목표주가 증권 리포트", 5)
        if target_news:
            parts.append(f"[증권가 목표주가 뉴스]\n" + "\n".join(f"• {n}" for n in target_news))

        # 3. 네이버 최신 뉴스
        if code:
            naver_news = get_naver_stock_news(code, 4)
            if naver_news:
                parts.append(f"[최신 뉴스]\n" + "\n".join(f"• {n}" for n in naver_news))

        # 4. Google News - 최신 뉴스 보충
        if not parts:
            fallback = get_google_news(f"{stock_name} 주식 실적 전망", 5)
            if fallback:
                parts.append(f"[최신 뉴스]\n" + "\n".join(f"• {n}" for n in fallback))

    return "\n\n".join(parts) if parts else f"{stock_name}: 리서치 데이터 없음"
