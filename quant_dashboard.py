"""
Streamlit 퀀트 대시보드
실행: streamlit run quant_dashboard.py
"""

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# .env 로드
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

from quant_engine import (
    compute_scores, generate_signals,
    run_backtest, days_ago_str, today_str, KOSPI_SAMPLE,
)
from quant_db import get_prices_cached, cache_stats
from quant_portfolio import get_market_filter, get_portfolio, get_current_prices

# ── 페이지 설정 ───────────────────────────────────────────────

st.set_page_config(
    page_title="KRX 퀀트 대시보드",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📈 KRX 퀀트 AI 투자 대시보드")
st.caption(f"업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

# ── 사이드바 ─────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ 설정")
    market  = st.selectbox("시장", ["KOSPI", "KOSDAQ"], index=0)
    top_n   = st.slider("상위 종목 수", 5, 30, 10)
    use_ml  = st.checkbox("LightGBM 모델 사용", value=False)
    use_sample = st.checkbox("대표 50종목만 사용 (빠름)", value=True)

    st.divider()
    st.subheader("💾 캐시 현황")
    try:
        stats = cache_stats()
        for k, v in stats.items():
            st.metric(k, v)
    except Exception:
        st.info("캐시 없음")

# ── 탭 구성 ──────────────────────────────────────────────────

tab_market, tab_signal, tab_portfolio, tab_backtest = st.tabs([
    "🌐 시장 현황", "🔍 팩터 신호", "💼 포트폴리오", "📊 백테스트"
])


# ═══════════════════════════════════════════════════════════
# TAB 1: 시장 현황
# ═══════════════════════════════════════════════════════════

with tab_market:
    st.subheader("시장 필터 (코스피 vs 60MA)")

    with st.spinner("코스피 데이터 로딩 중..."):
        mf = get_market_filter()

    col1, col2, col3 = st.columns(3)
    status_color = "🟢" if mf["status"] == "RISK_ON" else ("🔴" if mf["status"] == "RISK_OFF" else "⚪")
    col1.metric("시장 상태", f"{status_color} {mf['status']}")
    if mf.get("kospi"):
        col2.metric("코스피", f"{mf['kospi']:,.0f}")
        col3.metric("60MA 대비", f"{mf['pct_diff']:+.2f}%",
                    delta_color="normal" if mf["pct_diff"] >= 0 else "inverse")

    st.divider()
    st.subheader("주요 지수 & 매크로")

    import yfinance as yf

    @st.cache_data(ttl=300)
    def fetch_macro():
        tickers = {
            "코스피": "^KS11", "코스닥": "^KQ11",
            "S&P500": "^GSPC", "나스닥": "^IXIC",
            "VIX":    "^VIX",  "달러/원": "USDKRW=X",
            "WTI유가": "CL=F", "금": "GC=F",
            "미10년물": "^TNX",
        }
        rows = []
        for name, sym in tickers.items():
            try:
                info  = yf.Ticker(sym).fast_info
                price = info.last_price
                prev  = info.previous_close
                if price and prev:
                    pct = (price - prev) / prev * 100
                    rows.append({"지표": name, "현재": price, "등락(%)": pct})
            except Exception:
                pass
        return pd.DataFrame(rows)

    macro_df = fetch_macro()
    if not macro_df.empty:
        # 색상 포맷
        def color_pct(val):
            color = "color: #00c853" if val >= 0 else "color: #d32f2f"
            return color

        cols = st.columns(3)
        for i, (_, row) in enumerate(macro_df.iterrows()):
            c = cols[i % 3]
            sign  = "+" if row["등락(%)"] >= 0 else ""
            delta = f"{sign}{row['등락(%)']:.2f}%"
            c.metric(row["지표"], f"{row['현재']:,.2f}", delta=delta)


# ═══════════════════════════════════════════════════════════
# TAB 2: 팩터 신호
# ═══════════════════════════════════════════════════════════

with tab_signal:
    st.subheader("퀀트 팩터 스코어링")

    run_btn = st.button("🔄 신호 분석 실행", type="primary")

    if run_btn:
        tickers = KOSPI_SAMPLE if use_sample else KOSPI_SAMPLE  # 확장 가능

        with st.spinner(f"가격 데이터 수집 중 ({len(tickers)}종목)..."):
            prices = get_prices_cached(tickers, days_ago_str(200), today_str())

        if prices.empty:
            st.error("가격 데이터 수집 실패")
        else:
            with st.spinner("팩터 스코어 계산 중..."):
                scores  = compute_scores(prices)
                signals = generate_signals(scores, top_n=top_n)

            if use_ml:
                try:
                    from quant_model import predict, load_model, is_trained
                    if is_trained():
                        with st.spinner("LightGBM 예측 중..."):
                            ml_scores = predict(prices)
                        signals["lgb_score"] = ml_scores.reindex(signals.index)
                        st.success("LightGBM 점수 적용됨")
                    else:
                        st.warning("모델 미학습 — `python quant_model.py --train` 실행 필요")
                except Exception as e:
                    st.warning(f"LightGBM 로드 실패: {e}")

            # 신호 테이블
            display = signals.copy()
            display.index.name = "ticker"
            display = display.reset_index()

            signal_colors = {"BUY": "🟢", "SELL": "🔴", "NEUTRAL": "⚪"}
            display["신호"] = display["signal"].map(lambda s: f"{signal_colors.get(s,'⚪')} {s}")

            show_cols = ["rank", "종목명", "composite", "신호"]
            if "lgb_score" in display.columns:
                show_cols.append("lgb_score")

            st.dataframe(
                display[show_cols].rename(columns={
                    "rank": "순위", "composite": "팩터점수", "lgb_score": "ML점수"
                }),
                use_container_width=True,
                height=420,
            )

            # 팩터 점수 막대 차트
            top_df = scores.head(top_n).copy()
            top_df.index = top_df["종목명"].astype(str)
            factor_cols = [c for c in ["mom1m","mom3m","mom6m","ma_cross","rsi_rev"]
                           if c in top_df.columns]

            if factor_cols:
                st.subheader("팩터별 점수 분해")
                fig = px.bar(
                    top_df[factor_cols].reset_index(),
                    x="종목명", y=factor_cols,
                    barmode="group",
                    labels={"value": "점수(0~1)", "variable": "팩터"},
                    height=400,
                )
                fig.update_layout(xaxis_tickangle=-30)
                st.plotly_chart(fig, use_container_width=True)

            # composite 히트맵
            st.subheader("Composite 점수 히트맵")
            hmap_data = scores.head(20)[["종목명", "composite"]].copy()
            hmap_data["종목명"] = hmap_data["종목명"].astype(str)
            fig2 = px.bar(
                hmap_data, x="종목명", y="composite",
                color="composite", color_continuous_scale="RdYlGn",
                range_color=[0, 1], height=350,
            )
            fig2.update_layout(xaxis_tickangle=-30)
            st.plotly_chart(fig2, use_container_width=True)


# ═══════════════════════════════════════════════════════════
# TAB 3: 포트폴리오
# ═══════════════════════════════════════════════════════════

with tab_portfolio:
    st.subheader("보유 종목 현황")

    pf = get_portfolio()
    holdings = pf.get("holdings", {})

    col1, col2, col3 = st.columns(3)
    col1.metric("보유 종목 수", len(holdings))
    col2.metric("마지막 리밸런싱", pf.get("last_rebalance", "없음"))
    col3.metric("현금 모드", "🔴 ON" if pf.get("cash_mode") else "🟢 OFF")

    if holdings:
        with st.spinner("현재가 조회 중..."):
            cur_prices = get_current_prices(list(holdings.keys()))

        rows = []
        for ticker, info in holdings.items():
            entry = info.get("entry_price", 0)
            cur   = cur_prices.get(ticker, 0)
            ret   = (cur - entry) / entry * 100 if entry > 0 and cur > 0 else None
            rows.append({
                "종목코드":  ticker,
                "종목명":    info.get("name", ticker),
                "진입가":    entry,
                "현재가":    cur,
                "수익률(%)": round(ret, 2) if ret is not None else None,
                "비중":      f"{info.get('weight', 0)*100:.1f}%",
                "진입일":    info.get("entry_date", ""),
            })

        df_pf = pd.DataFrame(rows)
        st.dataframe(
            df_pf.style.background_gradient(
                subset=["수익률(%)"], cmap="RdYlGn", vmin=-20, vmax=20
            ),
            use_container_width=True,
            height=380,
        )

        # 수익률 막대 차트
        fig = px.bar(
            df_pf.dropna(subset=["수익률(%)"]),
            x="종목명", y="수익률(%)",
            color="수익률(%)", color_continuous_scale="RdYlGn",
            range_color=[-20, 20], height=350,
            title="종목별 수익률",
        )
        fig.add_hline(y=0, line_dash="dash", line_color="gray")
        fig.update_layout(xaxis_tickangle=-30)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("보유 종목 없음. `/rebalance apply` 또는 사이드바에서 리밸런싱을 실행하세요.")


# ═══════════════════════════════════════════════════════════
# TAB 4: 백테스트
# ═══════════════════════════════════════════════════════════

with tab_backtest:
    st.subheader("전략 백테스트")

    col1, col2, col3 = st.columns(3)
    bt_start  = col1.date_input("시작일", value=pd.Timestamp("2023-01-01"))
    bt_end    = col2.date_input("종료일", value=pd.Timestamp.today())
    bt_top_n  = col3.slider("상위 N종목", 5, 30, 10)

    bt_btn = st.button("▶️ 백테스트 실행", type="primary")

    if bt_btn:
        start_str = bt_start.strftime("%Y%m%d")
        end_str   = bt_end.strftime("%Y%m%d")

        with st.spinner("백테스트 실행 중 (데이터 수집 포함, 약 2~5분)..."):
            prices_bt = get_prices_cached(KOSPI_SAMPLE, start_str, end_str, verbose=False)
            result    = run_backtest(
                KOSPI_SAMPLE, start_str, end_str,
                top_n=bt_top_n, verbose=False,
            )

        if "error" in result:
            st.error(result["error"])
        else:
            # 성과 요약
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("총 수익률", f"{result['total_return']:+.1%}")
            c2.metric("CAGR",      f"{result['cagr']:+.1%}")
            c3.metric("MDD",       f"{result['mdd']:.1%}")
            c4.metric("Sharpe",    f"{result['sharpe']:.2f}")

            cc1, cc2 = st.columns(2)
            cc1.metric("월간 승률",   f"{result['win_rate']:.1%}")
            cc2.metric("리밸런싱 횟수", f"{result['n_rebalances']}회")

            # 수익 곡선
            pv = result["portfolio_values"]
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=pv.index, y=pv.values,
                mode="lines", name="퀀트 전략",
                line=dict(color="#2196F3", width=2),
                fill="tozeroy", fillcolor="rgba(33,150,243,0.1)",
            ))

            # 코스피 벤치마크
            try:
                import yfinance as yf
                bm = yf.Ticker("^KS11").history(
                    start=bt_start.strftime("%Y-%m-%d"),
                    end=bt_end.strftime("%Y-%m-%d"),
                )["Close"]
                bm_norm = bm / bm.iloc[0] * result["portfolio_values"].iloc[0]
                fig.add_trace(go.Scatter(
                    x=bm_norm.index, y=bm_norm.values,
                    mode="lines", name="KOSPI (벤치마크)",
                    line=dict(color="#FF9800", width=1.5, dash="dot"),
                ))
            except Exception:
                pass

            fig.update_layout(
                title="전략 vs 코스피 수익 곡선",
                xaxis_title="날짜", yaxis_title="포트폴리오 가치 (원)",
                height=420, hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)

            # 최근 보유 종목
            if result.get("holdings_log"):
                last_hold = result["holdings_log"][-1]
                st.subheader(f"마지막 리밸런싱 보유 종목 ({last_hold['date'].strftime('%Y-%m-%d')})")
                from quant_engine import get_ticker_name
                names = [f"{get_ticker_name(t)} ({t})" for t in last_hold["tickers"]]
                st.write(", ".join(names))
