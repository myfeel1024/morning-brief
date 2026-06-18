"""
미국 경기 국면 판단기
선행/동행/후행 지표 → 회복/성장/둔화/침체 국면 평가

출처:
  - FRED CSV (무료, API 키 불필요): fred.stlouisfed.org
  - yfinance: S&P 500

국면 기준:
  회복기 — 선행↑, 동행↓(바닥), 후행↓
  성장기 — 선행↑, 동행↑,  후행↑
  둔화기 — 선행↓, 동행↓전환, 후행↑
  침체기 — 선행↓, 동행↓,  후행↓
"""

import requests
import pandas as pd
import yfinance as yf
from io import StringIO
from datetime import datetime

_FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
_HEADERS   = {"User-Agent": "Mozilla/5.0"}

# ── FRED 시리즈 ID ───────────────────────────────────────────
# 선행
_ID_PMI    = "NAPM"           # ISM 제조업 PMI (월)
_ID_CSENT  = "UMCSENT"        # 미시간대 소비자심리지수 (월)
# 동행
_ID_INDPRO = "INDPRO"         # 산업생산지수 (월)
_ID_RETAIL = "RSAFS"          # 소매판매 (월, 백만달러)
# 후행
_ID_GDP    = "A191RL1Q225SBEA"  # 실질GDP 성장률 % SAAR (분기)
_ID_UNEMP  = "UNRATE"         # 실업률 (월)


def _fetch_fred(series_id: str, n: int = 12) -> pd.Series:
    """FRED에서 CSV 다운로드 후 최근 n 개 반환."""
    url = _FRED_BASE.format(series_id)
    r   = requests.get(url, timeout=40, headers=_HEADERS)
    r.raise_for_status()
    df  = pd.read_csv(StringIO(r.text), parse_dates=["DATE"], index_col="DATE")
    df  = df.replace(".", float("nan"))
    df.iloc[:, 0] = pd.to_numeric(df.iloc[:, 0], errors="coerce")
    return df.iloc[:, 0].dropna().tail(n)


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


_THR_UP  =  0.008   # 상승 판정 임계값
_THR_DN  = -0.008   # 하락 판정 임계값

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
    """
    실제 방향(L,C,G)과 국면 예상(eL,eC,eG) 일치도 점수.
    선행 가중 1.0, 동행 1.0, 후행 0.8
    """
    def score_one(actual, expected, w):
        if actual == expected:
            return w
        if actual == "fl":
            return 0.0
        return -w * 0.5

    return (score_one(L, eL, 1.0)
          + score_one(C, eC, 1.0)
          + score_one(G, eG, 0.8))


def get_econ_cycle() -> dict:
    """
    경기 국면 분석.
    반환값: {
      phase, phase_scores, leading_score, coincident_score, lagging_score,
      indicators, asset_advice, errors
    }
    """
    errors = []
    ind    = {}

    # ── 선행지표 ─────────────────────────────────────────────
    for key, fetch_fn, name in [
        ("sp500",  _fetch_sp500,               "S&P 500"),
        ("pmi",    lambda: _fetch_fred(_ID_PMI,   12), "ISM PMI"),
        ("csent",  lambda: _fetch_fred(_ID_CSENT, 12), "소비자심리지수"),
    ]:
        try:
            s = fetch_fn()
            ind[key] = {"name": name, "series": s, "trend": _trend(s)}
        except Exception as e:
            errors.append(f"{name}: {e}")
            ind[key] = {"name": name, "series": pd.Series(dtype=float), "trend": 0.0}

    # ── 동행지표 ─────────────────────────────────────────────
    for key, series_id, name in [
        ("indpro", _ID_INDPRO, "산업생산지수"),
        ("retail", _ID_RETAIL, "소매판매"),
    ]:
        try:
            s = _fetch_fred(series_id, 12)
            ind[key] = {"name": name, "series": s, "trend": _trend(s)}
        except Exception as e:
            errors.append(f"{name}: {e}")
            ind[key] = {"name": name, "series": pd.Series(dtype=float), "trend": 0.0}

    # ── 후행지표 ─────────────────────────────────────────────
    try:
        gdp = _fetch_fred(_ID_GDP, 8)   # 분기 데이터 → 8분기
        ind["gdp"] = {"name": "GDP 성장률(%)", "series": gdp, "trend": _trend(gdp, short=2, long=4)}
    except Exception as e:
        errors.append(f"GDP: {e}")
        ind["gdp"] = {"name": "GDP 성장률(%)", "series": pd.Series(dtype=float), "trend": 0.0}

    try:
        unemp = _fetch_fred(_ID_UNEMP, 12)
        # 실업률은 역행: 하락이 긍정 → 부호 반전
        unemp_trend = -_trend(unemp)
        ind["unemp"] = {"name": "실업률(%)", "series": unemp, "trend": unemp_trend}
    except Exception as e:
        errors.append(f"실업률: {e}")
        ind["unemp"] = {"name": "실업률(%)", "series": pd.Series(dtype=float), "trend": 0.0}

    # ── 그룹 종합 점수 ────────────────────────────────────────
    leading_score    = (ind["sp500"]["trend"] + ind["pmi"]["trend"] + ind["csent"]["trend"]) / 3
    coincident_score = (ind["indpro"]["trend"] + ind["retail"]["trend"]) / 2
    lagging_score    = (ind["gdp"]["trend"] + ind["unemp"]["trend"]) / 2

    L = _dir(leading_score)
    C = _dir(coincident_score)
    G = _dir(lagging_score)

    # ── 국면 판정 ─────────────────────────────────────────────
    # 회복: 선↑ 동↓(바닥) 후↓
    # 성장: 선↑ 동↑     후↑
    # 둔화: 선↓ 동↓전환  후↑
    # 침체: 선↓ 동↓     후↓
    phase_scores = {
        "회복기": _phase_match(L, C, G, "up", "dn", "dn"),
        "성장기": _phase_match(L, C, G, "up", "up", "up"),
        "둔화기": _phase_match(L, C, G, "dn", "dn", "up"),
        "침체기": _phase_match(L, C, G, "dn", "dn", "dn"),
    }
    phase = max(phase_scores, key=phase_scores.get)

    # ── 자산배분 조언 ─────────────────────────────────────────
    advice = {
        "회복기": "📈 *회복기*: 위험자산 비중 점진적 확대 시작 — 주식·리츠·원자재 분할 매수",
        "성장기": "🚀 *성장기*: 위험자산 최대 활용 — 주식·성장주 중심 포지션 유지",
        "둔화기": "⚠️ *둔화기*: 위험자산 비중 축소 — 채권·현금 확대, 방어주·배당주 전환",
        "침체기": "🛡️ *침체기*: 안전자산 위주 보유 — 국채·현금 유지, 저가 분할매수 준비",
    }[phase]

    return {
        "phase":            phase,
        "phase_scores":     phase_scores,
        "leading_score":    leading_score,
        "coincident_score": coincident_score,
        "lagging_score":    lagging_score,
        "indicators":       ind,
        "asset_advice":     advice,
        "errors":           errors,
    }


def format_econ_report(result: dict) -> str:
    """Telegram 마크다운 메시지 포맷."""
    now  = datetime.now()
    ind  = result["indicators"]

    def val_str(key, fmt=".1f") -> str:
        s = ind[key]["series"]
        if s.empty:
            return "N/A"
        v   = s.iloc[-1]
        idx = s.index[-1]
        dt  = idx.strftime("%y.%m") if hasattr(idx, "strftime") else ""
        return f"{v:{fmt}} ({dt})"

    def tl(key) -> str:
        return _trend_label(ind[key]["trend"])

    # 선행 종합 방향 (실업률만 부호가 반전돼 있으니 주의)
    # 후행 실업률: trend에 이미 -부호 적용됨, 표시용은 원래 방향(감소=좋음)
    unemp_display = _trend_label(-ind["unemp"]["trend"])   # 원래 방향으로 표시

    lines = [
        f"🌐 *미국 경기 국면 분석* — {now.strftime('%Y년 %m월')}",
        "",
        f"📌 현재 국면: *{result['phase']}*",
        "",
        "━━━ 🔮 선행지표 (미래 선행) ━━━",
        f"  📈 S&P500           {val_str('sp500', '.0f')}  {tl('sp500')}",
        f"  🏭 ISM PMI          {val_str('pmi')}  {tl('pmi')}",
        f"  😊 소비자심리지수   {val_str('csent')}  {tl('csent')}",
        f"  → 선행 종합: {_trend_label(result['leading_score'])}",
        "",
        "━━━ 📊 동행지표 (현재 상황) ━━━",
        f"  🏗️ 산업생산지수     {val_str('indpro')}  {tl('indpro')}",
        f"  🛒 소매판매($M)     {val_str('retail', '.0f')}  {tl('retail')}",
        f"  → 동행 종합: {_trend_label(result['coincident_score'])}",
        "",
        "━━━ 📉 후행지표 (결과 확인) ━━━",
        f"  🏛️ GDP 성장률(%)   {val_str('gdp')}  {tl('gdp')}",
        f"  👷 실업률(%)        {val_str('unemp')}  {unemp_display}",
        f"  → 후행 종합: {_trend_label(result['lagging_score'])}",
        "",
        "━━━ 국면 적합도 ━━━",
    ]

    for ph, sc in sorted(result["phase_scores"].items(), key=lambda x: -x[1]):
        marker = "◀" if ph == result["phase"] else " "
        lines.append(f"  {ph}: {sc:+.1f} {marker}")

    lines += [
        "",
        "💡 자산배분 조언",
        result["asset_advice"],
    ]

    if result["errors"]:
        lines.append(f"\n⚠️ 일부 데이터 조회 실패: {', '.join(result['errors'])}")

    return "\n".join(lines)
