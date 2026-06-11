"""
SQLite 로컬 캐시 — 가격/펀더멘털/신호 저장
pykrx 재호출 없이 저장된 데이터를 재사용해 속도 대폭 향상
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

DB_PATH = Path(__file__).resolve().parent / "quant_data.db"


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            date   TEXT NOT NULL,
            ticker TEXT NOT NULL,
            close  REAL,
            volume INTEGER,
            PRIMARY KEY (date, ticker)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS fundamentals (
            date   TEXT NOT NULL,
            ticker TEXT NOT NULL,
            pbr    REAL,
            per    REAL,
            roe    REAL,
            eps    REAL,
            PRIMARY KEY (date, ticker)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            date      TEXT NOT NULL,
            ticker    TEXT NOT NULL,
            name      TEXT,
            composite REAL,
            signal    TEXT,
            PRIMARY KEY (date, ticker)
        )
    """)
    con.commit()
    return con


# ── 가격 저장 / 로드 ──────────────────────────────────────────

def save_prices(df: pd.DataFrame) -> None:
    """DataFrame(날짜×종목) → prices 테이블에 upsert"""
    con = _conn()
    rows = [
        (idx.strftime("%Y-%m-%d"), col, float(val), None)
        for col in df.columns
        for idx, val in df[col].dropna().items()
    ]
    con.executemany(
        "INSERT OR REPLACE INTO prices (date, ticker, close, volume) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def load_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """캐시에서 가격 DataFrame 로드. start/end: YYYY-MM-DD"""
    con = _conn()
    ph  = ",".join("?" * len(tickers))
    df  = pd.read_sql(
        f"SELECT date, ticker, close FROM prices "
        f"WHERE ticker IN ({ph}) AND date >= ? AND date <= ?",
        con, params=[*tickers, start, end],
    )
    con.close()
    if df.empty:
        return pd.DataFrame()
    pivot = df.pivot(index="date", columns="ticker", values="close")
    pivot.index = pd.to_datetime(pivot.index)
    return pivot.sort_index()


def cached_tickers(start: str, end: str) -> list[str]:
    """캐시에 데이터가 있는 종목 목록"""
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT ticker FROM prices WHERE date >= ? AND date <= ?",
        (start, end),
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def is_fresh(ticker: str, min_rows: int = 100) -> bool:
    """해당 종목이 충분히 캐싱돼 있는지 확인"""
    con = _conn()
    cnt = con.execute(
        "SELECT COUNT(*) FROM prices WHERE ticker = ?", (ticker,)
    ).fetchone()[0]
    con.close()
    return cnt >= min_rows


# ── 펀더멘털 저장 / 로드 ──────────────────────────────────────

def save_fundamentals(df: pd.DataFrame, date: str) -> None:
    """DataFrame(ticker×지표) → fundamentals 테이블에 upsert"""
    con = _conn()
    col_map = {"PBR": "pbr", "PER": "per", "ROE": "roe", "EPS": "eps"}
    rows = []
    for ticker, row in df.iterrows():
        rows.append((
            date, ticker,
            float(row.get("PBR") or 0) or None,
            float(row.get("PER") or 0) or None,
            float(row.get("ROE") or 0) or None,
            float(row.get("EPS") or 0) or None,
        ))
    con.executemany(
        "INSERT OR REPLACE INTO fundamentals (date,ticker,pbr,per,roe,eps) VALUES (?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def load_fundamentals(tickers: list[str], date: str) -> pd.DataFrame:
    """캐시에서 펀더멘털 로드"""
    con = _conn()
    ph  = ",".join("?" * len(tickers))
    df  = pd.read_sql(
        f"SELECT ticker, pbr, per, roe, eps FROM fundamentals "
        f"WHERE ticker IN ({ph}) AND date = ?",
        con, params=[*tickers, date],
    )
    con.close()
    if df.empty:
        return pd.DataFrame()
    df = df.set_index("ticker")
    df.columns = [c.upper() for c in df.columns]
    return df


# ── 신호 저장 / 로드 ─────────────────────────────────────────

def save_signals(signals: pd.DataFrame, date: str) -> None:
    con = _conn()
    rows = []
    for ticker, row in signals.iterrows():
        rows.append((
            date, ticker,
            str(row.get("종목명", "")),
            float(row.get("composite", 0)),
            str(row.get("signal", "NEUTRAL")),
        ))
    con.executemany(
        "INSERT OR REPLACE INTO signals (date,ticker,name,composite,signal) VALUES (?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def load_signals(date: str) -> pd.DataFrame:
    con = _conn()
    df  = pd.read_sql(
        "SELECT ticker, name, composite, signal FROM signals WHERE date = ?",
        con, params=[date],
    )
    con.close()
    return df.set_index("ticker") if not df.empty else pd.DataFrame()


# ── 캐시 통계 ─────────────────────────────────────────────────

def cache_stats() -> dict:
    con = _conn()
    n_prices = con.execute("SELECT COUNT(DISTINCT ticker) FROM prices").fetchone()[0]
    n_dates  = con.execute("SELECT COUNT(DISTINCT date)   FROM prices").fetchone()[0]
    latest   = con.execute("SELECT MAX(date) FROM prices").fetchone()[0]
    n_fund   = con.execute("SELECT COUNT(DISTINCT ticker) FROM fundamentals").fetchone()[0]
    con.close()
    return {
        "종목 수":       n_prices,
        "날짜 수":       n_dates,
        "최신 날짜":     latest or "없음",
        "펀더멘털 종목": n_fund,
    }


# ── 스마트 가격 수집 (캐시 우선) ─────────────────────────────

def get_prices_cached(
    tickers: list[str],
    start_yyyymmdd: str,
    end_yyyymmdd: str,
    verbose: bool = False,
) -> pd.DataFrame:
    """
    캐시 우선 조회 → 없는 종목만 pykrx로 수집 후 저장.
    """
    from quant_engine import get_prices_batch

    start_iso = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:]}"
    end_iso   = f"{end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:]}"

    # 캐시에서 먼저 로드
    cached = load_prices(tickers, start_iso, end_iso)
    cached_set = set(cached.columns.tolist()) if not cached.empty else set()
    missing = [t for t in tickers if t not in cached_set or not is_fresh(t)]

    if missing:
        if verbose:
            print(f"  캐시 미스 {len(missing)}개 → pykrx 수집 중...")
        fresh = get_prices_batch(missing, start_yyyymmdd, end_yyyymmdd, verbose=verbose)
        if not fresh.empty:
            save_prices(fresh)
            if cached.empty:
                cached = fresh
            else:
                cached = cached.join(fresh, how="outer")

    return cached.sort_index() if not cached.empty else pd.DataFrame()
