"""
forecast_recheck.py — Periodický recheck předpovědí
=====================================================
Spouštěn každé 3 hodiny (z run.py nebo cron).

Co dělá:
  1. Načte všechny OPEN pozice.
  2. Pro každou zjistí aktuální předpověď (stejný provider chain jako daily_buy).
  3. Porovná s uloženou predicted_temp.
  4. Pokud rozdíl překračí DIVERGE_THRESHOLD → označí pozici jako forecast_diverged.
  5. V monitor_positions.py pak takto označená pozice čeká na zisk ≥ PROFIT_TAKE_PCT
     (výchozí 50 %) místo pevné tržní ceny 0.50.

Konfigurace (.env):
  FORECAST_DIVERGE_THRESHOLD_F=5.0   # min rozdíl v °F (výchozí 5°F)
  FORECAST_DIVERGE_THRESHOLD_C=2.5   # min rozdíl v °C (výchozí 2.5°C)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from weather_api import WeatherCollector, CITY_MAP
from ledger import PaperLedger, Trade

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(os.getenv("BOT_DATA_DIR", Path(__file__).parent.parent / "data")).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "forecast_recheck.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("forecast_recheck")

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------

# Minimální teplotní rozdíl pro označení jako "diverged"
DIVERGE_THRESHOLD_F = float(os.getenv("FORECAST_DIVERGE_THRESHOLD_F", "5.0"))
DIVERGE_THRESHOLD_C = float(os.getenv("FORECAST_DIVERGE_THRESHOLD_C", "2.5"))


def _diverge_threshold(unit: str) -> float:
    return DIVERGE_THRESHOLD_F if unit.upper() == "F" else DIVERGE_THRESHOLD_C


# ---------------------------------------------------------------------------
# Hlavní funkce
# ---------------------------------------------------------------------------

def run_forecast_recheck() -> dict:
    """
    Porovná aktuální předpovědi s uloženými hodnotami pro všechny OPEN pozice.
    Označí divergující pozice → monitor bude čekat na P&L zisk, ne pevnou cenu.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info("=== FORECAST RECHECK === %s", now_iso)

    ledger = PaperLedger()
    collector = WeatherCollector(
        meteoblue_api_key=os.getenv("METEOBLUE_API_KEY", ""),
    )

    open_trades = ledger.get_open_trades()
    if not open_trades:
        logger.info("Žádné otevřené pozice ke kontrole.")
        return {
            "checked_at": now_iso,
            "open_positions": 0,
            "checked": 0,
            "newly_diverged": 0,
            "still_aligned": 0,
            "reconciled": 0,
            "errors": [],
        }

    logger.info("Kontroluji %d otevřených pozic…", len(open_trades))

    results = {
        "checked_at": now_iso,
        "open_positions": len(open_trades),
        "checked": 0,
        "newly_diverged": 0,   # nově označeny jako diverged
        "still_aligned": 0,    # předpověď stále souhlasí
        "reconciled": 0,       # byly diverged, ale teď souhlasí (vyčistěno)
        "errors": [],
        "positions": [],
    }

    for trade in open_trades:
        pos_result = _check_trade_forecast(trade, collector, ledger)
        results["positions"].append(pos_result)
        results["checked"] += 1

        action = pos_result["action"]
        if action == "NEWLY_DIVERGED":
            results["newly_diverged"] += 1
        elif action == "STILL_ALIGNED":
            results["still_aligned"] += 1
        elif action == "RECONCILED":
            results["reconciled"] += 1
        elif action == "ERROR":
            results["errors"].append(pos_result.get("error", ""))

    _print_summary(results)
    return results


def _check_trade_forecast(
    trade: Trade,
    collector: WeatherCollector,
    ledger: PaperLedger,
) -> dict:
    """Zkontroluje jednu pozici a porovná aktuální předpověď s uloženou."""

    logger.info(
        "Kontroluji: %s | entry_forecast=%.1f°%s | target=%s",
        trade.city, trade.predicted_temp, trade.unit, trade.target_date,
    )

    # Cílové datum z trade (předáváme jako date object)
    try:
        target_date = date.fromisoformat(trade.target_date)
    except ValueError as exc:
        return {"trade_id": trade.id, "city": trade.city, "action": "ERROR",
                "error": f"Špatné datum: {exc}"}

    # Zjisti aktuální předpověď
    try:
        forecast = collector.get_forecast(trade.city, target_date)
    except Exception as exc:
        error_msg = f"Chyba předpovědi: {exc}"
        logger.warning("  %s: %s", trade.city, error_msg)
        return {"trade_id": trade.id, "city": trade.city, "action": "ERROR",
                "error": error_msg}

    if not forecast:
        return {"trade_id": trade.id, "city": trade.city, "action": "ERROR",
                "error": "Předpověď nedostupná"}

    current_forecast = forecast.predicted_high
    entry_forecast   = trade.predicted_temp
    diff             = abs(current_forecast - entry_forecast)
    threshold        = _diverge_threshold(trade.unit)
    is_diverged      = diff >= threshold

    logger.info(
        "  Entry=%.1f°%s | Nyní=%.1f°%s | Δ=%.1f°%s | práh=%.1f°%s | diverged=%s",
        entry_forecast, trade.unit,
        current_forecast, trade.unit,
        diff, trade.unit,
        threshold, trade.unit,
        is_diverged,
    )

    # Bylo dříve diverged ale teď nesouhlasí → reconcile (vychýlení se vrátilo)
    if trade.forecast_diverged and not is_diverged:
        ledger.mark_forecast_diverged(trade.id, current_forecast, diverged=False)
        return {
            "trade_id": trade.id,
            "city": trade.city,
            "action": "RECONCILED",
            "entry_forecast": entry_forecast,
            "current_forecast": current_forecast,
            "diff": round(diff, 1),
            "unit": trade.unit,
        }

    # Nově diverged
    if is_diverged and not trade.forecast_diverged:
        ledger.mark_forecast_diverged(trade.id, current_forecast, diverged=True)
        direction = "↑" if current_forecast > entry_forecast else "↓"
        logger.warning(
            "  ⚠️  FORECAST DIVERGED: %s %.1f°%s → %.1f°%s (%s%.1f°)",
            trade.city, entry_forecast, trade.unit,
            current_forecast, trade.unit, direction, diff,
        )
        return {
            "trade_id": trade.id,
            "city": trade.city,
            "action": "NEWLY_DIVERGED",
            "entry_forecast": entry_forecast,
            "current_forecast": current_forecast,
            "diff": round(diff, 1),
            "diff_direction": "up" if current_forecast > entry_forecast else "down",
            "unit": trade.unit,
        }

    # Stále diverged (žádná změna)
    if is_diverged and trade.forecast_diverged:
        ledger.mark_forecast_diverged(trade.id, current_forecast, diverged=True)
        return {
            "trade_id": trade.id,
            "city": trade.city,
            "action": "STILL_DIVERGED",
            "entry_forecast": entry_forecast,
            "current_forecast": current_forecast,
            "diff": round(diff, 1),
            "unit": trade.unit,
        }

    # Stále aligned (no change)
    ledger.mark_forecast_diverged(trade.id, current_forecast, diverged=False)
    return {
        "trade_id": trade.id,
        "city": trade.city,
        "action": "STILL_ALIGNED",
        "entry_forecast": entry_forecast,
        "current_forecast": current_forecast,
        "diff": round(diff, 1),
        "unit": trade.unit,
    }


# ---------------------------------------------------------------------------
# Výpis
# ---------------------------------------------------------------------------

def _print_summary(results: dict) -> None:
    print("\n" + "~"*55)
    print(f"🔄 FORECAST RECHECK — {results['checked_at'][:19]}Z")
    print("~"*55)
    print(f"   Kontrolováno pozic: {results['checked']}")
    print(f"   Nově diverged:      {results['newly_diverged']}")
    print(f"   Stále aligned:      {results['still_aligned']}")
    print(f"   Reconciled:         {results['reconciled']}")

    if results.get("positions"):
        print(f"\n   DETAILY:")
        for pos in results["positions"]:
            action = pos["action"]
            icons = {
                "NEWLY_DIVERGED": "⚠️ ",
                "STILL_DIVERGED": "🔶",
                "RECONCILED":     "✅",
                "STILL_ALIGNED":  "📐",
                "ERROR":          "❌",
            }
            icon = icons.get(action, "❔")
            if action in ("NEWLY_DIVERGED", "STILL_DIVERGED"):
                direction = "↑" if pos.get("diff_direction") == "up" else "↓"
                print(
                    f"   {icon} {pos['city']:12s} entry={pos['entry_forecast']:.1f}°"
                    f"{pos['unit']} → now={pos['current_forecast']:.1f}°{pos['unit']} "
                    f"({direction}{pos['diff']:.1f}°) → čeká na P&L≥50%"
                )
            elif action == "RECONCILED":
                print(
                    f"   {icon} {pos['city']:12s} forecast zpět na {pos['current_forecast']:.1f}°"
                    f"{pos['unit']} (diff={pos['diff']:.1f}°) → normální exit"
                )
            elif action == "STILL_ALIGNED":
                print(
                    f"   {icon} {pos['city']:12s} {pos['current_forecast']:.1f}°"
                    f"{pos['unit']} (Δ={pos['diff']:.1f}°) ✓"
                )
            elif action == "ERROR":
                print(f"   {icon} {pos['city']:12s} {pos.get('error', '')}")

    if results["errors"]:
        print(f"\n   ⚠️  CHYBY:")
        for e in results["errors"]:
            if e:
                print(f"      • {e}")

    print("~"*55 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_forecast_recheck()

    output_file = LOG_DIR / f"forecast_recheck_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    sys.exit(0 if not result["errors"] else 1)