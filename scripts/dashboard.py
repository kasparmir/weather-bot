"""
dashboard.py — Streamlit Web Dashboard
Vizualizace paper trading dat v reálném čase.

Spuštění:
    streamlit run dashboard.py

Env proměnná BOT_DATA_DIR nastaví cestu k datovým souborům.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Konfigurace stránky
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Polymarket Weather Bot",
    page_icon="🌡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", Path(__file__).parent.parent / "data"))
TRADES_CSV = DATA_DIR / "trades.csv"
PORTFOLIO_JSON = DATA_DIR / "portfolio.json"
BALANCE_HISTORY_CSV = DATA_DIR / "balance_history.csv"
REFRESH_INTERVAL = 30  # sekund


# ---------------------------------------------------------------------------
# Helper funkce pro načítání dat
# ---------------------------------------------------------------------------

@st.cache_data(ttl=REFRESH_INTERVAL)
def load_trades() -> pd.DataFrame:
    if not TRADES_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(TRADES_CSV)
        if df.empty:
            return df
        # Konverze typů
        for col in ["entry_price", "current_price", "exit_price", "pnl", "pnl_pct", "predicted_temp"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        for col in ["timestamp", "entry_timestamp", "exit_timestamp", "last_checked"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
        return df
    except Exception as e:
        st.error(f"Chyba načítání trades: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=REFRESH_INTERVAL)
def load_portfolio() -> dict:
    if not PORTFOLIO_JSON.exists():
        return {
            "balance": 1000.0,
            "total_invested": 0,
            "total_pnl": 0,
            "win_rate": 0,
            "wins": 0,
            "losses": 0,
            "current_equity": 1000.0,
            "total_return_pct": 0,
        }
    try:
        with open(PORTFOLIO_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        st.error(f"Chyba načítání portfolia: {e}")
        return {}


@st.cache_data(ttl=REFRESH_INTERVAL)
def load_balance_history() -> pd.DataFrame:
    if not BALANCE_HISTORY_CSV.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(BALANCE_HISTORY_CSV)
        if df.empty:
            return df
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
        df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
        df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
        return df.dropna(subset=["timestamp", "balance"])
    except Exception as e:
        st.error(f"Chyba načítání balance historie: {e}")
        return pd.DataFrame()


def get_pnl_color(pnl: float) -> str:
    if pnl > 0:
        return "🟢"
    elif pnl < 0:
        return "🔴"
    return "⚪"


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

def render_sidebar(portfolio: dict) -> None:
    with st.sidebar:
        st.title("🌡️ Weather Bot")
        st.caption("Polymarket Paper Trading")

        st.divider()

        # Portfolio summary v sidebaru
        st.metric(
            "💼 Balance",
            f"${portfolio.get('balance', 0):.2f}",
            delta=f"{portfolio.get('total_return_pct', 0):+.2f}%",
        )
        st.metric("📈 Equity", f"${portfolio.get('current_equity', 0):.2f}")
        st.metric(
            "🎯 P&L celkem",
            f"${portfolio.get('total_pnl', 0):.2f}",
            delta=None,
        )

        st.divider()

        win_rate = portfolio.get("win_rate", 0)
        wins = portfolio.get("wins", 0)
        losses = portfolio.get("losses", 0)
        st.metric("✅ Win rate", f"{win_rate:.1f}%")
        st.caption(f"Wins: {wins} | Losses: {losses}")

        st.divider()

        # Auto-refresh toggle
        auto_refresh = st.checkbox("🔄 Auto-refresh (30s)", value=True)
        if auto_refresh:
            time.sleep(0.1)
            st.caption(f"Poslední update: {datetime.now().strftime('%H:%M:%S')}")

        st.divider()
        st.caption("Data directory:")
        st.code(str(DATA_DIR), language=None)


# ---------------------------------------------------------------------------
# Sekce: Otevřené pozice
# ---------------------------------------------------------------------------

def render_open_positions(df: pd.DataFrame) -> None:
    st.header("📊 Otevřené pozice")

    if df.empty:
        st.info("Žádné otevřené pozice.")
        return

    open_df = df[df["status"] == "OPEN"].copy()
    if open_df.empty:
        st.info("Žádné otevřené pozice.")
        return

    # Výpočet live P&L
    open_df["unrealized_pnl"] = (
        (open_df["current_price"] / open_df["entry_price"] - 1) * 10
    ).round(4)
    open_df["unrealized_pnl_pct"] = (
        (open_df["current_price"] / open_df["entry_price"] - 1) * 100
    ).round(2)
    open_df["distance_to_50pct"] = (0.50 - open_df["current_price"]).round(4)

    # Metriky souhrnu
    cols = st.columns(4)
    with cols[0]:
        st.metric("Počet pozic", len(open_df))
    with cols[1]:
        total_unrealized = open_df["unrealized_pnl"].sum()
        st.metric(
            "Nezrealizovaný P&L",
            f"${total_unrealized:.2f}",
            delta=f"{total_unrealized:.2f}",
        )
    with cols[2]:
        avg_price = open_df["current_price"].mean()
        st.metric("Průměrná cena YES", f"{avg_price:.4f}")
    with cols[3]:
        nearest = open_df.loc[open_df["distance_to_50pct"].idxmin()]
        st.metric(
            "Nejblíže k profit-take",
            nearest["city"],
            delta=f"{nearest['current_price']:.4f} ({nearest['distance_to_50pct']:+.4f})",
        )

    # Detailní tabulka
    st.subheader("Detaily pozic")
    display_cols = {
        "city": "Město",
        "target_date": "Datum",
        "predicted_temp": "Předpověď",
        "unit": "Jed.",
        "entry_price": "Entry",
        "current_price": "Aktuální",
        "unrealized_pnl": "Nezr. P&L ($)",
        "unrealized_pnl_pct": "Nezr. P&L (%)",
        "distance_to_50pct": "Do 50%",
        "market_slug": "Slug",
    }

    display_df = open_df[[c for c in display_cols if c in open_df.columns]].copy()
    display_df = display_df.rename(columns=display_cols)

    # Barevné zvýraznění
    def color_pnl(val):
        try:
            v = float(val)
            if v > 0:
                return "background-color: #d4edda; color: #155724"
            elif v < 0:
                return "background-color: #f8d7da; color: #721c24"
        except Exception:
            pass
        return ""

    styled = display_df.style.applymap(color_pnl, subset=["Nezr. P&L ($)"] if "Nezr. P&L ($)" in display_df.columns else [])
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Sekce: Balance historie
# ---------------------------------------------------------------------------

def render_balance_chart(balance_df: pd.DataFrame) -> None:
    st.header("📈 Vývoj balance")

    if balance_df.empty:
        st.info("Žádná data o balance — spusť denní nákup.")
        return

    try:
        import plotly.graph_objects as go

        fig = go.Figure()

        # Balance line
        fig.add_trace(go.Scatter(
            x=balance_df["timestamp"],
            y=balance_df["balance"],
            name="Balance",
            line=dict(color="#2196F3", width=2),
            fill="tonexty",
        ))

        # Equity line (pokud existuje)
        if "equity" in balance_df.columns:
            fig.add_trace(go.Scatter(
                x=balance_df["timestamp"],
                y=balance_df["equity"],
                name="Equity (balance + otevřené)",
                line=dict(color="#4CAF50", width=2, dash="dot"),
            ))

        # Počáteční balance reference
        fig.add_hline(
            y=1000.0, line_dash="dash", line_color="gray",
            annotation_text="Počáteční balance ($1000)",
        )

        fig.update_layout(
            title="Vývoj portfolia v čase",
            xaxis_title="Datum",
            yaxis_title="USD",
            hovermode="x unified",
            height=400,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig, use_container_width=True)

    except ImportError:
        # Fallback: Streamlit native chart
        chart_df = balance_df.set_index("timestamp")[["balance"]].dropna()
        st.line_chart(chart_df)


# ---------------------------------------------------------------------------
# Sekce: Uzavřené obchody
# ---------------------------------------------------------------------------

def render_closed_trades(df: pd.DataFrame) -> None:
    st.header("📋 Uzavřené obchody")

    if df.empty:
        st.info("Žádné uzavřené obchody.")
        return

    closed_df = df[df["status"] != "OPEN"].copy()
    if closed_df.empty:
        st.info("Žádné uzavřené obchody.")
        return

    # Celkové statistiky
    total_pnl = closed_df["pnl"].sum()
    wins = (closed_df["pnl"] > 0).sum()
    losses = (closed_df["pnl"] <= 0).sum()
    win_rate = wins / len(closed_df) * 100 if len(closed_df) > 0 else 0

    cols = st.columns(5)
    with cols[0]:
        st.metric("Celkový P&L", f"${total_pnl:.2f}", delta=f"{total_pnl:.2f}")
    with cols[1]:
        st.metric("Uzavřeno pozic", len(closed_df))
    with cols[2]:
        st.metric("Win rate", f"{win_rate:.1f}%")
    with cols[3]:
        st.metric("✅ Wins", wins)
    with cols[4]:
        st.metric("❌ Losses", losses)

    # Tabulka obchodů
    display_cols = {
        "city": "Město",
        "target_date": "Datum",
        "predicted_temp": "Teplota",
        "unit": "Jed.",
        "entry_price": "Entry",
        "exit_price": "Exit",
        "pnl": "P&L ($)",
        "pnl_pct": "P&L (%)",
        "status": "Status",
        "exit_timestamp": "Uzavřeno",
    }

    display_df = closed_df[[c for c in display_cols if c in closed_df.columns]].copy()
    if "exit_timestamp" in display_df.columns:
        display_df["exit_timestamp"] = display_df["exit_timestamp"].dt.strftime("%Y-%m-%d %H:%M")
    display_df = display_df.rename(columns=display_cols)

    def highlight_status(row):
        styles = [""] * len(row)
        if "Status" in row.index:
            s = row["Status"]
            if "PROFIT" in str(s):
                styles = ["background-color: #d4edda"] * len(row)
            elif "SETTLEMENT" in str(s):
                styles = ["background-color: #fff3cd"] * len(row)
        return styles

    styled = display_df.style.apply(highlight_status, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Sekce: Přesnost predikcí
# ---------------------------------------------------------------------------

def render_prediction_accuracy(df: pd.DataFrame) -> None:
    st.header("🎯 Přesnost predikcí")

    if df.empty:
        st.info("Nedostatek dat pro analýzu přesnosti.")
        return

    closed_df = df[df["status"] != "OPEN"].copy()
    if closed_df.empty:
        st.info("Žádné uzavřené pozice pro analýzu.")
        return

    st.info(
        "📝 **Poznámka**: Přesnost predikcí (předpověď vs. skutečná teplota) "
        "bude zobrazena po implementaci zpětného doplnění reálných dat po settlementu. "
        "Prozatím vidíte analýzu P&L per město."
    )

    # P&L per město
    city_stats = (
        closed_df.groupby("city")
        .agg(
            obchodů=("pnl", "count"),
            celkový_pnl=("pnl", "sum"),
            průměrný_pnl=("pnl", "mean"),
            wins=("pnl", lambda x: (x > 0).sum()),
        )
        .reset_index()
    )
    city_stats["win_rate %"] = (city_stats["wins"] / city_stats["obchodů"] * 100).round(1)
    city_stats["celkový_pnl"] = city_stats["celkový_pnl"].round(2)
    city_stats["průměrný_pnl"] = city_stats["průměrný_pnl"].round(4)

    st.dataframe(city_stats.set_index("city"), use_container_width=True)

    # Distribuce entry cen
    try:
        import plotly.express as px
        fig = px.histogram(
            closed_df,
            x="entry_price",
            color="status",
            nbins=20,
            title="Distribuce entry cen",
            labels={"entry_price": "Entry cena (YES prob.)", "count": "Počet"},
        )
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:
        pass


# ---------------------------------------------------------------------------
# Hlavní dashboard
# ---------------------------------------------------------------------------

def main() -> None:
    # Načtení dat
    df = load_trades()
    portfolio = load_portfolio()
    balance_history = load_balance_history()

    # Sidebar
    render_sidebar(portfolio)

    # Hlavní obsah
    st.title("🌡️ Polymarket Weather Bot — Dashboard")
    st.caption(
        f"Paper trading dashboard | "
        f"Data: `{DATA_DIR}` | "
        f"Poslední refresh: {datetime.now().strftime('%H:%M:%S')}"
    )

    if not TRADES_CSV.exists():
        st.warning(
            "⚠️ Soubor `trades.csv` nenalezen. "
            "Spusť nejprve `python scripts/daily_buy.py` pro zahájení obchodování.",
            icon="⚠️",
        )
        st.code(f"python {Path(__file__).parent}/daily_buy.py", language="bash")
        return

    # Záložky
    tab1, tab2, tab3, tab4 = st.tabs([
        "📊 Otevřené pozice",
        "📈 Balance history",
        "📋 Uzavřené obchody",
        "🎯 Přesnost predikcí",
    ])

    with tab1:
        render_open_positions(df)

    with tab2:
        render_balance_chart(balance_history)

    with tab3:
        render_closed_trades(df)

    with tab4:
        render_prediction_accuracy(df)

    # Auto-refresh
    st.divider()
    if st.button("🔄 Obnovit data nyní"):
        st.cache_data.clear()
        st.rerun()


if __name__ == "__main__":
    main()
