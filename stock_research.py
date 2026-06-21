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
        res.encoding = "cp949"   # 네이버 리서치 페이지는 EUC-KR/CP949 — UTF-8로 강제하면 한글이 깨짐
        soup = BeautifulSoup(res.text, "html.parser")

        # 테이블 컬럼 순서(고정): [0]종목명 [1]제목 [2]증권사 [3]투자의견 [4]작성일(YY.MM.DD) [5]목표가
        reports = []
        for row in soup.select("tr"):
            cols = row.find_all("td")
            if len(cols) < 6:
                continue
            texts = [c.get_text(strip=True) for c in cols]
            title = texts[1]   # [0]은 종목명 링크, [1]이 실제 리포트 제목
            if not title or len(title) < 2:
                continue

            firm        = texts[2]
            opinion_raw = texts[3]
            date_raw    = texts[4]
            target_raw  = texts[5]

            target = ""
            if re.match(r"^[\d,]+$", target_raw) and len(target_raw.replace(",", "")) >= 4:
                target = target_raw + "원"

            date = date_raw if re.match(r"\d{2}\.\d{2}\.\d{2}", date_raw) else ""

            reports.append({
                "title": title[:60],
                "firm": firm,
                "target_price": target,
                "opinion": opinion_raw,
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


# ── 국내 경제/증시 뉴스 (RSS 멀티소스) ──────────────────────────

# 국내 주요 경제지 RSS 피드 목록
_KR_NEWS_RSS = [
    # 연합뉴스 경제
    ("연합뉴스", "https://www.yna.co.kr/rss/economy.xml"),
    # 한국경제
    ("한국경제", "https://www.hankyung.com/feed/economy"),
    # 매일경제
    ("매일경제", "https://www.mk.co.kr/rss/30200030/"),
    # 머니투데이
    ("머니투데이", "https://rss.mt.co.kr/mt_news_eco.xml"),
]

def _parse_rss(url: str, source: str, n: int = 4) -> list[str]:
    """단일 RSS 피드에서 헤드라인 파싱"""
    try:
        res = requests.get(url, timeout=8,
                           headers={"User-Agent": "Mozilla/5.0"})
        res.encoding = "utf-8"
        root = ET.fromstring(res.content)
        items = root.findall(".//item")
        results = []
        for item in items[:n]:
            title_el = item.find("title")
            if title_el is not None and title_el.text:
                title = title_el.text.strip()
                if title and len(title) > 5:
                    results.append(f"{title} ({source})")
        return results
    except Exception:
        return []


def fetch_korean_news(n: int = 8) -> list[str]:
    """
    국내 경제/증시 뉴스 수집.
    RSS 피드 → 실패 시 Google News RSS 폴백.
    """
    seen, results = set(), []

    # 1차: 국내 경제지 RSS
    for source, url in _KR_NEWS_RSS:
        for item in _parse_rss(url, source, n=3):
            if item not in seen and len(results) < n:
                seen.add(item)
                results.append(item)
        if len(results) >= n:
            break

    # 2차: 부족하면 Google News RSS (한국어) 보충
    if len(results) < n:
        for query in ["코스피 코스닥 증시", "한국은행 금리 환율 물가"]:
            for item in get_google_news(query, n=4):
                if item not in seen and len(results) < n:
                    seen.add(item)
                    results.append(item)

    return results


# ── 한국 주식 실시간 데이터 (yfinance .KS/.KQ) ───────────────

def _get_kr_price_naver(stock_code: str) -> dict:
    """네이버 금융 실시간 polling API — 장중에도 수 초~수십 초 지연 수준.
    pykrx(장마감 후 확정치)·yfinance(15~20분 지연)보다 장중 정확도가 높음.
    """
    try:
        url = f"https://polling.finance.naver.com/api/realtime/domestic/stock/{stock_code}"
        res = requests.get(url, timeout=5, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://finance.naver.com/",
            "Accept": "application/json, text/plain, */*",
        })
        if res.status_code != 200:
            print(f"[naver_polling] {stock_code} HTTP {res.status_code}", flush=True)
            return {}
        data  = res.json()
        datas = data.get("datas", [])
        if not datas:
            print(f"[naver_polling] {stock_code} 빈 응답: {data}", flush=True)
            return {}
        d = datas[0]
        price_raw = d.get("closePrice", "")
        price = float(str(price_raw).replace(",", "")) if price_raw else 0.0
        if price <= 0:
            print(f"[naver_polling] {stock_code} price<=0: {d}", flush=True)
            return {}
        diff_raw = d.get("compareToPreviousClosePrice", "")
        diff = float(str(diff_raw).replace(",", "")) if diff_raw not in (None, "") else None
        sign = (d.get("compareToPreviousPrice") or {}).get("code", "")
        if diff is not None and sign == "5" and diff > 0:
            diff = -diff
        pct_raw = d.get("fluctuationsRatio", "")
        pct = float(pct_raw) if pct_raw not in (None, "") else None
        if pct is not None and sign == "5" and pct > 0:
            pct = -pct
        prev = (price - diff) if diff is not None else None
        return {"price": price, "prev": prev, "diff": diff, "pct": pct}
    except Exception as e:
        print(f"[naver_polling] {stock_code} 실패: {e}", flush=True)
        return {}


def _get_kr_price_naver_mobile(stock_code: str) -> dict:
    """네이버 모바일 증권 API — polling API가 차단될 때의 2차 네이버 소스."""
    try:
        url = f"https://m.stock.naver.com/api/stock/{stock_code}/basic"
        res = requests.get(url, timeout=5, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": f"https://m.stock.naver.com/domestic/stock/{stock_code}/total",
            "Accept": "application/json, text/plain, */*",
        })
        if res.status_code != 200:
            print(f"[naver_mobile] {stock_code} HTTP {res.status_code}", flush=True)
            return {}
        d = res.json()
        price_raw = d.get("closePrice", "")
        price = float(str(price_raw).replace(",", "")) if price_raw else 0.0
        if price <= 0:
            return {}
        diff_raw = d.get("compareToPreviousClosePrice", "")
        diff = float(str(diff_raw).replace(",", "")) if diff_raw not in (None, "") else None
        sign = (d.get("compareToPreviousPrice") or {}).get("code", "")
        if diff is not None and sign == "5" and diff > 0:
            diff = -diff
        pct_raw = d.get("fluctuationsRatio", "")
        pct = float(pct_raw) if pct_raw not in (None, "") else None
        if pct is not None and sign == "5" and pct > 0:
            pct = -pct
        prev = (price - diff) if diff is not None else None
        return {"price": price, "prev": prev, "diff": diff, "pct": pct}
    except Exception as e:
        print(f"[naver_mobile] {stock_code} 실패: {e}", flush=True)
        return {}


def _get_kr_price_pykrx(stock_code: str) -> float:
    """pykrx로 한국 주식 현재가(당일 or 전일 종가) 조회."""
    try:
        from pykrx import stock as krx
        from datetime import datetime, timedelta
        for delta in [0, 1, 2, 3]:
            d = (datetime.now() - timedelta(days=delta)).strftime("%Y%m%d")
            df = krx.get_market_ohlcv_by_date(d, d, stock_code)
            if not df.empty:
                return float(df["종가"].iloc[-1])
    except Exception as e:
        print(f"[pykrx_price] {stock_code} 실패: {e}", flush=True)
    return 0.0


def get_kr_stock_realtime(stock_code: str) -> dict:
    """
    한국 주식 실시간 데이터 조회.
    가격: 네이버 polling → 네이버 모바일 → pykrx(장마감 확정치) → yfinance 순.
    재무지표: yfinance.
    """
    print(f"[KR price] {stock_code} 조회 시작", flush=True)

    # ── Step 1: 네이버 실시간 polling ──
    naver = _get_kr_price_naver(stock_code)
    price = naver.get("price", 0) or 0
    source = "naver_polling" if price > 0 else ""

    # ── Step 2: 네이버 모바일 (polling 실패 시 2차 소스) ──
    if price <= 0:
        naver = _get_kr_price_naver_mobile(stock_code)
        price = naver.get("price", 0) or 0
        if price > 0:
            source = "naver_mobile"

    # ── Step 3: pykrx (네이버 둘 다 실패 시 — 장마감 후 확정치) ──
    if price <= 0:
        price = _get_kr_price_pykrx(stock_code)
        if price > 0:
            source = "pykrx"

    # ── Step 4: yfinance (재무지표 + 가격 최종 보조) ──
    yf_fi   = None
    yf_info = {}
    for suffix in [".KS", ".KQ"]:
        try:
            t  = yf.Ticker(f"{stock_code}{suffix}")
            fi = t.fast_info
            yf_price = fi.last_price or fi.previous_close
            if not yf_price or yf_price <= 0:
                hist = t.history(period="2d", interval="1d")
                if not hist.empty:
                    yf_price = float(hist["Close"].iloc[-1])
            if yf_price and yf_price > 0:
                if price <= 0:
                    price = yf_price   # 네이버·pykrx 모두 실패 시 사용
                    source = "yfinance"
                yf_fi   = fi
                yf_info = t.info
                break
        except Exception as e:
            print(f"[yfinance_price] {stock_code}{suffix} 실패: {e}", flush=True)
            continue

    print(f"[KR price] {stock_code} source={source or 'FAILED'} price={price}", flush=True)

    if not price or price <= 0:
        return {}

    result = {"현재가": f"{price:,.0f}원"}

    # 등락 — 네이버 실시간 데이터 우선, 없으면 yfinance 전일종가로 계산
    if naver.get("diff") is not None and naver.get("pct") is not None:
        diff, pct = naver["diff"], naver["pct"]
        arrow = "▲" if diff >= 0 else "▼"
        result["등락"] = f"{arrow} {abs(diff):,.0f}원 ({pct:+.2f}%)"
    elif yf_fi and yf_fi.previous_close and yf_fi.previous_close > 0:
        prev  = yf_fi.previous_close
        diff  = price - prev
        pct   = diff / prev * 100
        arrow = "▲" if diff >= 0 else "▼"
        result["등락"] = f"{arrow} {abs(diff):,.0f}원 ({pct:+.2f}%)"

    # 52주 고저 + 현재 위치 (yfinance)
    if yf_fi:
        try:
            high52 = yf_fi.fifty_two_week_high
            low52  = yf_fi.fifty_two_week_low
            if high52 and low52 and high52 > low52:
                result["52주 고가"] = f"{high52:,.0f}원"
                result["52주 저가"] = f"{low52:,.0f}원"
                pos = (price - low52) / (high52 - low52) * 100
                result["52주 내 위치"] = f"{pos:.0f}%  (0%=저점, 100%=고점)"
        except Exception:
            pass

    # 재무 지표 (yfinance)
    if yf_info:
        try:
            mc = yf_info.get("marketCap")
            if mc and mc > 0:
                result["시가총액"] = (
                    f"{mc/1e12:.2f}조원" if mc >= 1e12 else f"{mc/1e8:.0f}억원"
                )
            pe = yf_info.get("trailingPE")
            if pe and pe > 0:
                result["PER(TTM)"] = f"{pe:.1f}배"
            pb = yf_info.get("priceToBook")
            if pb and pb > 0:
                result["PBR"] = f"{pb:.2f}배"
            eps = yf_info.get("trailingEps")
            if eps and eps != 0:
                result["EPS"] = f"{eps:,.0f}원"
            dy = yf_info.get("dividendYield")
            if dy and dy > 0:
                result["배당수익률"] = f"{dy*100:.2f}%"
            vol = yf_fi.volume if yf_fi else None
            if vol and vol > 0:
                result["거래량"] = f"{vol:,}주"
            # 애널리스트 목표주가
            target_mean = yf_info.get("targetMeanPrice")
            target_high = yf_info.get("targetHighPrice")
            target_low  = yf_info.get("targetLowPrice")
            if target_mean and target_mean > 0:
                result["애널리스트 평균목표주가"] = f"{target_mean:,.0f}원"
                upside = (target_mean - price) / price * 100
                result["목표주가 upside"] = f"{upside:+.1f}%"
            if target_low and target_high and target_low > 0:
                result["목표주가 범위"] = f"{target_low:,.0f}원 ~ {target_high:,.0f}원"
        except Exception:
            pass

    return result


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

        # 1. 실시간 시세 & 재무 데이터
        if code:
            realtime = get_kr_stock_realtime(code)
            if realtime:
                lines = "\n".join(f"  {k}: {v}" for k, v in realtime.items())
                parts.append(f"[실시간 시세 & 재무 - {stock_name}]\n{lines}")

        # 2. 네이버 금융 증권사 리포트 + 목표주가
        if code:
            reports = get_naver_analyst_reports(code, 5)
            if reports:
                lines = []
                # 현재가 대비 목표주가 upside 계산
                current_price = 0
                try:
                    price_str = realtime.get("현재가", "").replace(",", "").replace("원", "")
                    if price_str.isdigit():
                        current_price = float(price_str)
                except Exception:
                    pass

                for r in reports:
                    line = f"  [{r['firm'] or '?'}] {r['title']}"
                    if r["target_price"]:
                        try:
                            tp = float(r["target_price"].replace(",", "").replace("원", ""))
                            # 현재가 대비 5배 초과 또는 1/5 미만 목표주가는 액면분할·급등 등으로
                            # 시점이 어긋난 과거 리포트일 가능성이 높아 upside 계산 없이 표시만
                            implausible = current_price > 0 and (
                                tp > current_price * 5 or tp < current_price * 0.2
                            )
                            if implausible:
                                line += f" | 목표주가 {r['target_price']} (※시점 불일치로 upside 계산 제외)"
                            else:
                                upside = (tp - current_price) / current_price * 100 if current_price > 0 else 0
                                line += f" | 목표주가 {r['target_price']} ({upside:+.1f}%)"
                        except Exception:
                            line += f" | 목표주가 {r['target_price']}"
                    if r["opinion"]:
                        line += f" | {r['opinion']}"
                    if r["date"]:
                        line += f" ({r['date']})"
                    lines.append(line)
                parts.append(f"[증권사 리포트 - {stock_name}]\n" + "\n".join(lines))

        # 3. Google News - 목표주가 헤드라인
        target_news = get_google_news(f"{stock_name} 목표주가 증권 리포트", 5)
        if target_news:
            parts.append(f"[증권가 목표주가 뉴스]\n" + "\n".join(f"• {n}" for n in target_news))

        # 4. 네이버 최신 뉴스
        if code:
            naver_news = get_naver_stock_news(code, 4)
            if naver_news:
                parts.append(f"[최신 뉴스]\n" + "\n".join(f"• {n}" for n in naver_news))

        # 5. Google News - 최신 뉴스 보충
        if len(parts) <= 1:
            fallback = get_google_news(f"{stock_name} 주식 실적 전망", 5)
            if fallback:
                parts.append(f"[최신 뉴스]\n" + "\n".join(f"• {n}" for n in fallback))

    return "\n\n".join(parts) if parts else f"{stock_name}: 리서치 데이터 없음"
