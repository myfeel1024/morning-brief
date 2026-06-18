"""
미국 경기 국면 판단기
선행/동행/후행 지표 → 회복/성장/둔화/침체 국면 평가

출처: NH투자증권 백창규 센터장 프레임워크 기반
데이터: FRED API (공식, 무료 API 키 필요) + yfinance

환경변수: FRED_API_KEY (https://fred.stlouisfed.org/docs/api/api_key.html 무료 발급)

국면 기준:
  회복기 — 선행↑, 동행↓(바닥), 후행↓  → 주도: 대형 성장주(M7), IT·반도체, 미국 선진국
  성장기 — 선행↑, 동행↑, 후행↑       → 주도: 신흥국(한국·중국), 시클리컬, 원자재
  둔화기 — 선행↓, 동행↓전환, 후행↑    → 주도: 미국 방어주(통신·제약·유틸리티·필수소비재)
  침체기 — 선행↓, 동행↓, 후행↓       → 주도: 달러·국채·메가캡(현금흐름 우수 초대형주)

모니터링 주기: 매월 21~25일 (미국 주요 지표 발표 시점)
"""

import os
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# ── FRED API 설정 ─────────────────────────────────────────────
_FRED_API_KEY  = os.getenv("FRED_API_KEY", "")
_FRED_API_BASE = "https://api.stlouisfed.org/fred/series/observations"

# ── FRED 시리즈 ID ───────────────────────────────────────────
# 선행지표
_ID_PMI     = "GACDFSA066MSFRBPHI"  # 필라델피아 연준 제조업 현황 일반활동 확산지수 (월, SA)
_ID_CSENT   = "UMCSENT"         # 미시간대 소비자심리지수 (월)
_ID_SPREAD  = "T10Y2Y"          # 장단기 금리차 10Y-2Y (일별)
# 동행지표
_ID_INDPRO  = "INDPRO"          # 산업생산지수 (월)
_ID_RETAIL  = "RSAFS"           # 소매판매 (월, 백만달러)
_ID_CAPU    = "TCU"             # 설비 가동률 (월, %)
_ID_EXPORT  = "BOPTEXP"         # 수출액 (월, 백만달러) — 글로벌 수요 동행 지표
# 후행지표
_ID_GDP     = "A191RL1Q225SBEA" # 실질GDP 성장률 % SAAR (분기)
_ID_UNEMP   = "UNRATE"          # 실업률 (월)
_ID_WAGE    = "CES0500000003"   # 시간당 평균임금 (월, 달러)
_ID_HOURS   = "AWHAETP"         # 주당 평균 근로시간 (월, 전체 민간부문)

# ── 국면별 추천 섹터·종목 ─────────────────────────────────────
_PHASE_TICKERS = {
    "회복기": [
        ("🖥️", "IT·S/W",     ["MSFT", "CRM", "NOW", "ADBE"]),
        ("🔬", "반도체",      ["NVDA", "AMD", "AVGO", "TSM"]),
        ("💬", "커뮤니케이션", ["META", "GOOGL", "NFLX"]),
        ("🛍️", "자유소비재",  ["AMZN", "TSLA", "NKE"]),
        ("✈️", "운송",        ["UPS", "FDX", "DAL"]),
    ],
    "성장기": [
        ("🏦", "금융",             ["JPM", "GS", "MS", "BAC"]),
        ("🏗️", "산업재",          ["CAT", "DE", "GE", "HON"]),
        ("💊", "헬스케어(바이오·기기)", ["LLY", "MRNA", "ABT", "SYK"]),
        ("⚡", "에너지·소재",      ["XOM", "CVX", "LIN", "FCX"]),
        ("🏢", "리츠",             ["PLD", "O", "SPG"]),
        ("🎬", "미디어·엔터",      ["DIS", "SPOT", "PARA"]),
    ],
    "둔화기": [
        ("📡", "통신",        ["VZ", "T", "TMUS"]),
        ("💉", "헬스케어(제약)", ["JNJ", "PFE", "MRK", "ABBV"]),
        ("🛒", "필수소비재",  ["PG", "KO", "WMT", "CL"]),
        ("💡", "유틸리티",    ["NEE", "DUK", "SO"]),
    ],
    "침체기": [
        ("🏰", "메가캡",   ["AAPL", "MSFT", "GOOGL", "AMZN", "BRK-B"]),
        ("💡", "유틸리티", ["NEE", "DUK", "SO", "AEP"]),
        ("📡", "통신",     ["VZ", "T", "TMUS"]),
    ],
}


def _fetch_fred(series_id: str, n: int = 24) -> pd.Series:
    """FRED 공식 API로 시계열 조회 후 최근 n개 반환."""
    if not _FRED_API_KEY:
        raise ValueError("FRED_API_KEY 환경변수가 설정되지 않았습니다.")
    params = {
        "series_id":  series_id,
        "api_key":    _FRED_API_KEY,
        "file_type":  "json",
        "sort_order": "desc",   # 최신 데이터부터 내림차순
        "limit":      n * 2,    # 결측치 여유분 포함
    }
    r = requests.get(_FRED_API_BASE, params=params, timeout=30)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    dates, values = [], []
    for o in obs:
        if o["value"] != ".":
            dates.append(o["date"])
            values.append(float(o["value"]))
    # 내림차순으로 받았으므로 역순 정렬해서 시계열 복원
    s = pd.Series(list(reversed(values)), index=pd.to_datetime(list(reversed(dates))))
    return s.tail(n)


def _fetch_fred_monthly_avg(series_id: str, n: int = 12) -> pd.Series:
    """일별 데이터를 월평균으로 리샘플 후 최근 n개 반환 (장단기 금리차 등)."""
    s = _fetch_fred(series_id, n=500)
    if s.empty:
        return s
    monthly = s.resample("ME").mean()
    return monthly.tail(n)


def _fetch_sp500(n: int = 7) -> pd.Series:
    """yfinance ^GSPC 월간 종가."""
    hist = yf.Ticker("^GSPC").history(period="2y", interval="1mo")
    if hist.empty:
        return pd.Series(dtype=float)
    return hist["Close"].tail(n)


def _trend(series: pd.Series, short: int = 3, long: int = 6) -> float:
    """
    최근 short개월 평균 vs 이전 (long-short)개월 평균 비교.
    양수 = 상승 추세, 음수 = 하락 추세
    """
    if len(series) < long:
        return 0.0
    recent = series.iloc[-short:].mean()
    prior  = series.iloc[-long:-short].mean()
    if prior == 0:
        return 0.0
    return (recent - prior) / abs(prior)


_THR_UP =  0.008
_THR_DN = -0.008


def _dir(t: float) -> str:
    """trend float → 'up' / 'dn' / 'fl'"""
    if t > _THR_UP:
        return "up"
    if t < _THR_DN:
        return "dn"
    return "fl"


def _trend_label(t: float) -> str:
    d = _dir(t)
    if d == "up":
        return "↑ 상승"
    if d == "dn":
        return "↓ 하락"
    return "→ 보합"


def _phase_match(L: str, C: str, G: str,
                 eL: str, eC: str, eG: str) -> float:
    """실제 방향과 국면 예상 일치도 점수. 선행 가중 1.2, 동행 1.0, 후행 0.8"""
    def s(actual, expected, w):
        if actual == expected:
            return w
        if actual == "fl":
            return 0.0
        return -w * 0.5
    return s(L, eL, 1.2) + s(C, eC, 1.0) + s(G, eG, 0.8)


def get_econ_cycle() -> dict:
    """
    경기 국면 분석.
    반환값: {phase, phase_scores, leading_score, coincident_score,
             lagging_score, indicators, asset_advice, errors}
    """
    errors = []
    ind    = {}

    # ── 선행지표 ─────────────────────────────────────────────
    try:
        sp = _fetch_sp500(7)
        ind["sp500"] = {"name": "S&P 500", "series": sp, "trend": _trend(sp)}
    except Exception as e:
        errors.append(f"S&P500: {e}")
        ind["sp500"] = {"name": "S&P 500", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        pmi = _fetch_fred(_ID_PMI, 12)
        ind["pmi"] = {"name": "필라델피아 PMI", "series": pmi, "trend": _trend(pmi)}
    except Exception as e:
        errors.append(f"PMI: {e}")
        ind["pmi"] = {"name": "필라델피아 PMI", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        cs = _fetch_fred(_ID_CSENT, 12)
        ind["csent"] = {"name": "소비자심리지수", "series": cs, "trend": _trend(cs)}
    except Exception as e:
        errors.append(f"소비자심리지수: {e}")
        ind["csent"] = {"name": "소비자심리지수", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        spread = _fetch_fred_monthly_avg(_ID_SPREAD, 12)
        # 장단기 금리차: 양수(정상), 음수(역전=경기침체 신호)
        # 역전→회복 방향으로 개선되는 추세도 중요
        ind["spread"] = {"name": "장단기 금리차(10Y-2Y)", "series": spread, "trend": _trend(spread)}
    except Exception as e:
        errors.append(f"장단기 금리차: {e}")
        ind["spread"] = {"name": "장단기 금리차(10Y-2Y)", "series": pd.Series(dtype=float), "trend": 0.0}

    # ── 동행지표 ─────────────────────────────────────────────
    try:
        ip = _fetch_fred(_ID_INDPRO, 12)
        ind["indpro"] = {"name": "산업생산지수", "series": ip, "trend": _trend(ip)}
    except Exception as e:
        errors.append(f"산업생산지수: {e}")
        ind["indpro"] = {"name": "산업생산지수", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        rs = _fetch_fred(_ID_RETAIL, 12)
        ind["retail"] = {"name": "소매판매", "series": rs, "trend": _trend(rs)}
    except Exception as e:
        errors.append(f"소매판매: {e}")
        ind["retail"] = {"name": "소매판매", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        cu = _fetch_fred(_ID_CAPU, 12)
        ind["capu"] = {"name": "설비 가동률(%)", "series": cu, "trend": _trend(cu)}
    except Exception as e:
        errors.append(f"설비 가동률: {e}")
        ind["capu"] = {"name": "설비 가동률(%)", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        ex = _fetch_fred(_ID_EXPORT, 12)
        ind["export"] = {"name": "수출액($M)", "series": ex, "trend": _trend(ex)}
    except Exception as e:
        errors.append(f"수출: {e}")
        ind["export"] = {"name": "수출액($M)", "series": pd.Series(dtype=float), "trend": 0.0}

    # ── 후행지표 ─────────────────────────────────────────────
    try:
        gdp = _fetch_fred(_ID_GDP, 8)
        ind["gdp"] = {"name": "GDP 성장률(%)", "series": gdp, "trend": _trend(gdp, short=2, long=4)}
    except Exception as e:
        errors.append(f"GDP: {e}")
        ind["gdp"] = {"name": "GDP 성장률(%)", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        un = _fetch_fred(_ID_UNEMP, 12)
        ind["unemp"] = {"name": "실업률(%)", "series": un, "trend": -_trend(un)}  # 역행
    except Exception as e:
        errors.append(f"실업률: {e}")
        ind["unemp"] = {"name": "실업률(%)", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        wg = _fetch_fred(_ID_WAGE, 12)
        ind["wage"] = {"name": "시간당 평균임금($)", "series": wg, "trend": _trend(wg)}
    except Exception as e:
        errors.append(f"임금: {e}")
        ind["wage"] = {"name": "시간당 평균임금($)", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        hr = _fetch_fred(_ID_HOURS, 12)
        ind["hours"] = {"name": "주당 근로시간(h)", "series": hr, "trend": _trend(hr)}
    except Exception as e:
        errors.append(f"주당근로시간: {e}")
        ind["hours"] = {"name": "주당 근로시간(h)", "series": pd.Series(dtype=float), "trend": 0.0}

    # ── 그룹 종합 점수 ────────────────────────────────────────
    # 선행 4개: 주가, PMI, 소비자심리, 장단기금리차
    leading_score    = (ind["sp500"]["trend"] + ind["pmi"]["trend"]
                        + ind["csent"]["trend"] + ind["spread"]["trend"]) / 4
    # 동행 4개: 산업생산, 소매판매, 설비가동률, 수출
    coincident_score = (ind["indpro"]["trend"] + ind["retail"]["trend"]
                        + ind["capu"]["trend"]  + ind["export"]["trend"]) / 4
    # 후행 4개: GDP, 실업률(역행), 임금, 주당근로시간
    lagging_score    = (ind["gdp"]["trend"] + ind["unemp"]["trend"]
                        + ind["wage"]["trend"] + ind["hours"]["trend"]) / 4

    L = _dir(leading_score)
    C = _dir(coincident_score)
    G = _dir(lagging_score)

    # ── 국면 판정 ─────────────────────────────────────────────
    phase_scores = {
        "회복기": _phase_match(L, C, G, "up", "dn", "dn"),
        "성장기": _phase_match(L, C, G, "up", "up", "up"),
        "둔화기": _phase_match(L, C, G, "dn", "dn", "up"),
        "침체기": _phase_match(L, C, G, "dn", "dn", "dn"),
    }
    phase = max(phase_scores, key=phase_scores.get)

    # ── 경보 감지 ─────────────────────────────────────────────
    sp500_falling      = ind["sp500"]["trend"] < -_THR_UP
    coincident_turning = coincident_score < -_THR_UP

    # 핵심 경보: 주가 조정 + 동행지표 하락 전환 동시 발생
    # → 성장기→둔화기 진입의 가장 명확한 신호
    slowdown_warning = sp500_falling and coincident_turning

    # 조기 경보: 성장기 내에서 동행지표가 소프트닝 시작
    growth_to_slowdown = (phase == "성장기" and coincident_score < 0)

    # ── 국면별 조언 ────────────────────────────────────────────
    # 투자 전략 원칙:
    #   성장기→둔화기 진입: 핵심 경보 → 위험자산 즉시 축소, 현금 확보
    #   둔화기: 현금 유지하면서 바닥 탐색, 분할매수 준비
    #   침체기: 오히려 조금씩 주식을 담으면서 현금 줄이는 단계 (공포에 사라)
    #   회복기: 위험자산 비중 본격 확대
    # 지역 강세: 성장기만 EM 우위 / 나머지 3개 국면은 미국 주식 우위
    # 현대 사이클: 연준·정부 개입으로 둔화→회복 단축이 잦으나 데이터가 우선
    advice_map = {
        "회복기": (
            "📈 *회복기* — 위험자산 비중 본격 확대\n"
            "• 주도 섹터: 대형 성장주(M7), IT·소프트웨어, 반도체\n"
            "• 주도 지역: *미국·선진국* 중심 (신흥국 대비 미국 우위)\n"
            "• 침체기에 담아둔 주식의 비중을 더 늘릴 타이밍\n"
            "• 연준 금리 인하 본격화 → 성장주·리츠 등 금리 민감 자산 수혜"
        ),
        "성장기": (
            "🚀 *성장기* — 위험자산 최대 활용\n"
            "• 주도 섹터: 시클리컬(소재·에너지·산업재), 원자재\n"
            "• 주도 지역: *신흥국(한국·중국)* 강세 — EM이 미국을 outperform하는 유일한 국면\n"
            "• 글로벌 주문 증가 → 수출 중심 종목·ETF 비중 확대\n"
            "• ⚠️ 동행지표가 꺾이는 순간을 항상 모니터링 — 그것이 매도 신호"
        ),
        "둔화기": (
            "⚠️ *둔화기* — 현금 비중 유지하며 바닥 탐색\n"
            "• 주도 섹터: 방어주 (통신·제약·바이오·유틸리티·필수소비재)\n"
            "• 주도 지역: *미국* 우위 (신흥국 약세, 달러 강세 경향)\n"
            "• 지금은 현금을 최대한 지키는 단계 — 조급하게 들어가지 않기\n"
            "• 바닥 탐색 기준: PMI가 50 아래에서 반등하고 주가가 먼저 움직이면 신호\n"
            "• 시나리오 A (현대 사이클): 연준 선제 금리 인하 → 침체 없이 회복 복귀\n"
            "• 시나리오 B: 후행지표까지 하락 전환되면 침체기 진입 → 현금 유지\n"
            "• 어느 시나리오든 선행지표(주가·PMI)가 먼저 반등할 때 소량 분할매수 시작"
        ),
        "침체기": (
            "🛡️ *침체기* — 현금 줄이며 주식을 조금씩 담는 단계\n"
            "• 주도 자산: 달러·미국 국채 (핵심 안전판 유지)\n"
            "• 주도 종목: *미국 메가캡* (부채 낮고 현금흐름 우수한 초대형주부터)\n"
            "• 주도 지역: *미국* 절대 우위, 신흥국 아직 회피\n"
            "• 공포가 극대화될 때 분할매수 — 한 번에 다 사지 말고 3~5회 나눠서\n"
            "• 매수 신호 체크리스트:\n"
            "  ① 선행지표(주가·PMI)가 저점 대비 반등하기 시작\n"
            "  ② 연준 금리 인하 또는 양적완화 시그널\n"
            "  ③ 장단기 금리차가 역전에서 정상화 방향으로 전환\n"
            "• 현대 사이클: 연준·정부 개입으로 침체 기간이 단축되는 경향\n"
            "  → 회복기 진입 신호를 놓치지 않는 것이 핵심"
        ),
    }

    return {
        "phase":              phase,
        "phase_scores":       phase_scores,
        "leading_score":      leading_score,
        "coincident_score":   coincident_score,
        "lagging_score":      lagging_score,
        "indicators":         ind,
        "asset_advice":       advice_map[phase],
        "slowdown_warning":   slowdown_warning,
        "growth_to_slowdown": growth_to_slowdown,
        "errors":             errors,
    }


def format_econ_report(result: dict) -> str:
    """Telegram 마크다운 메시지 포맷."""
    now = datetime.now()
    ind = result["indicators"]

    def val(key, fmt=".1f") -> str:
        s = ind[key]["series"]
        if s.empty:
            return "N/A"
        v   = s.iloc[-1]
        idx = s.index[-1]
        dt  = idx.strftime("%y.%m") if hasattr(idx, "strftime") else ""
        return f"{v:{fmt}} ({dt})"

    def tl(key) -> str:
        return _trend_label(ind[key]["trend"])

    # 실업률·임금: trend는 이미 역행/일반 처리됨, 표시는 원래 방향
    unemp_raw = _trend_label(-ind["unemp"]["trend"])

    # ── 경보 배너 ─────────────────────────────────────────────
    warning_lines = []
    if result.get("slowdown_warning"):
        warning_lines += [
            "🚨 *[경보] 성장기→둔화기 전환 신호 감지*",
            "주가 조정 + 동행지표 하락 전환이 동시 확인됐습니다.",
            "→ *지금이 핵심 매도 타이밍 — 위험자산 즉시 축소, 현금 확보*",
            "→ 이 신호를 놓치면 이후 대응이 어려워집니다.",
            "",
        ]
    elif result.get("growth_to_slowdown"):
        warning_lines += [
            "⚠️ *[주의] 성장기 내 동행지표 둔화 조짐*",
            "아직 둔화기 전환은 아니나 동행지표가 꺾이기 시작했습니다.",
            "→ 수익 일부 실현하고 방망이 짧게 잡기 시작 권고",
            "",
        ]

    lines = [
        f"🌐 *미국 경기 국면 분석* — {now.strftime('%Y년 %m월')}",
        "",
    ] + warning_lines + [
        f"📌 현재 국면: *{result['phase']}*",
        "",
        "━━━ 🔮 선행지표 (미래 선행) ━━━",
        f"  📈 S\\&P500              {val('sp500', '.0f')}  {tl('sp500')}",
        f"  🏭 필라델피아 PMI        {val('pmi', '.1f')}  {tl('pmi')}",
        f"  😊 소비자심리지수       {val('csent')}  {tl('csent')}",
        f"  📐 장단기 금리차(%)     {val('spread', '.2f')}  {tl('spread')}",
        f"  → 선행 종합: *{_trend_label(result['leading_score'])}*",
        "",
        "━━━ 📊 동행지표 (현재 상황) ━━━",
        f"  🏗️ 산업생산지수         {val('indpro')}  {tl('indpro')}",
        f"  🛒 소매판매($M)         {val('retail', '.0f')}  {tl('retail')}",
        f"  ⚙️ 설비 가동률(%)       {val('capu')}  {tl('capu')}",
        f"  🚢 수출액($M)           {val('export', '.0f')}  {tl('export')}",
        f"  → 동행 종합: *{_trend_label(result['coincident_score'])}*",
        "",
        "━━━ 📉 후행지표 (결과 확인) ━━━",
        f"  🏛️ GDP 성장률(%)       {val('gdp')}  {tl('gdp')}",
        f"  👷 실업률(%)            {val('unemp')}  {unemp_raw}",
        f"  💵 시간당 임금($)       {val('wage')}  {tl('wage')}",
        f"  ⏱️ 주당 근로시간(h)     {val('hours')}  {tl('hours')}",
        f"  → 후행 종합: *{_trend_label(result['lagging_score'])}*",
        "",
        "━━━ 국면 적합도 ━━━",
    ]

    for ph in ["회복기", "성장기", "둔화기", "침체기"]:
        sc     = result["phase_scores"][ph]
        marker = " ◀ 현재" if ph == result["phase"] else ""
        lines.append(f"  {ph}: {sc:+.1f}{marker}")

    lines += [
        "",
        "━━━ 💡 자산배분 조언 ━━━",
        result["asset_advice"],
        "",
        "━━━ 📋 추천 섹터·종목 ━━━",
    ]

    phase = result["phase"]
    for emoji, sector, tickers in _PHASE_TICKERS.get(phase, []):
        lines.append(f"  {emoji} {sector}: {', '.join(tickers)}")

    lines += [
        "",
        "📅 _매월 28일 자동 브리핑 | 출처: FRED + yfinance_",
    ]

    if result["errors"]:
        err_summary = ", ".join(e.split(":")[0] for e in result["errors"])
        lines.append(f"⚠️ 데이터 조회 실패: {err_summary}")

    return "\n".join(lines)
