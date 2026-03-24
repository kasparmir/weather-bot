"""
daily_buy.py — Denní nákupní skript
Spouštěn každý den v 18:00 UTC přes OpenClaw cron.

Workflow:
  1. Získá předpovědi pro všechna města (NOAA + Meteoblue)
  2. Najde odpovídající kontrakty na Polymarketu
  3. Otevře papírové pozice
  4. Zapíše výsledky do logu (čitelné pro OpenClaw)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

# Přidáme scripts/ do Python path pokud spouštíme přímo
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from weather_api import WeatherCollector, WeatherForecast
from polymarket_gamma import PolymarketGamma
from ledger import PaperLedger, TRADES_CSV, PORTFOLIO_JSON, BALANCE_HISTORY_CSV, DATA_DIR

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
        logging.FileHandler(LOG_DIR / "daily_buy.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("daily_buy")


# ---------------------------------------------------------------------------
# Hlavní funkce
# ---------------------------------------------------------------------------

def run_daily_buy() -> dict:
    """
    Spustí denní nákupní cyklus.
    Vrátí summary dict (OpenClaw ho vypíše jako zprávu).
    """
    tomorrow = date.today() + timedelta(days=1)
    logger.info("=== DENNÍ NÁKUP === Target date: %s", tomorrow)

    collector = WeatherCollector(
        meteoblue_api_key=os.getenv("METEOBLUE_API_KEY", ""),
    )
    gamma = PolymarketGamma()
    ledger = PaperLedger()

    results = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "target_date": tomorrow.isoformat(),
        "forecasts_fetched": 0,
        "markets_found": 0,
        "positions_opened": 0,
        "positions_skipped": 0,
        "errors": [],
        "trades": [],
        "portfolio_balance": ledger.portfolio.balance,
    }

    # --- 1. Získej předpovědi počasí ---
    logger.info("Získávám předpovědi počasí pro %d měst...", 10)
    forecasts = collector.get_all_forecasts(tomorrow)
    results["forecasts_fetched"] = len(forecasts)
    logger.info("Získáno %d předpovědí", len(forecasts))

    if not forecasts:
        msg = "Žádné předpovědi nebyly získány — kontroluj API klíče"
        logger.error(msg)
        results["errors"].append(msg)
        return results

    # --- 2. Pro každou předpověď najdi Polymarket kontrakt a nakup ---
    for forecast in forecasts:
        try:
            result = _process_forecast(forecast, gamma, ledger, tomorrow)
            if result:
                results["trades"].append(result)
                if result["action"] == "OPENED":
                    results["markets_found"] += 1
                    results["positions_opened"] += 1
                elif result["action"] == "SKIPPED":
                    results["positions_skipped"] += 1
                elif result["action"] == "NO_MARKET":
                    pass  # Trh nenalezen, ok
        except Exception as exc:
            error_msg = f"Chyba pro {forecast.city}: {exc}"
            logger.error(error_msg, exc_info=True)
            results["errors"].append(error_msg)

    # --- 3. Aktualizuj finální stats ---
    results["portfolio_balance"] = round(ledger.portfolio.balance, 2)

    # --- 4. Vypíš human-readable summary (pro OpenClaw output) ---
    _print_summary(results, forecasts)

    return results


def _process_forecast(
    forecast: WeatherForecast,
    gamma: PolymarketGamma,
    ledger: PaperLedger,
    target_date: date,
) -> dict | None:
    """Zpracuje jednu předpověď — najde trh a otevře pozici."""

    # Najdi Polymarket trh
    from weather_api import CITY_MAP
    city_cfg = CITY_MAP.get(forecast.city)
    polymarket_name = city_cfg.polymarket_name if city_cfg else forecast.city.lower().replace(" ", "-")

    logger.info(
        "Hledám trh: %s | předpověď %.1f°%s",
        forecast.city, forecast.predicted_high, forecast.unit
    )

    market = gamma.find_weather_market(
        city_polymarket_name=polymarket_name,
        target_date=target_date,
        predicted_temp=forecast.predicted_high,
        unit=forecast.unit,
    )

    if not market:
        logger.info("  → Trh nenalezen pro %s", forecast.city)
        return {
            "city": forecast.city,
            "action": "NO_MARKET",
            "predicted_temp": forecast.predicted_high,
            "unit": forecast.unit,
            "reason": "market_not_found",
        }

    logger.info(
        "  → Nalezen: %s | YES cena: %.1f%%",
        market.market_slug, market.yes_price_pct
    )

    # Kontrola: cena by měla být obchodovatelná (0.02–0.98)
    entry_price = market.best_ask if market.best_ask > 0 else market.yes_price
    if entry_price <= 0.02 or entry_price >= 0.98:
        reason = f"entry_price mimo rozsah: {entry_price:.4f}"
        logger.info("  → Přeskakuji: %s", reason)
        return {
            "city": forecast.city,
            "action": "SKIPPED",
            "market_slug": market.market_slug,
            "predicted_temp": forecast.predicted_high,
            "unit": forecast.unit,
            "entry_price": entry_price,
            "reason": reason,
        }

    # Otevři pozici
    trade = ledger.open_position(
        city=forecast.city,
        target_date=target_date,
        predicted_temp=forecast.predicted_high,
        unit=forecast.unit,
        market_slug=market.market_slug,
        market_question=market.question,
        entry_price=entry_price,
    )

    if not trade:
        return {
            "city": forecast.city,
            "action": "SKIPPED",
            "market_slug": market.market_slug,
            "predicted_temp": forecast.predicted_high,
            "unit": forecast.unit,
            "entry_price": entry_price,
            "reason": "ledger_rejected",
        }

    return {
        "city": forecast.city,
        "action": "OPENED",
        "trade_id": trade.id,
        "market_slug": market.market_slug,
        "predicted_temp": forecast.predicted_high,
        "unit": forecast.unit,
        "entry_price": entry_price,
        "yes_price_pct": market.yes_price_pct,
    }


def _print_summary(results: dict, forecasts: list[WeatherForecast]) -> None:
    """Vypíše summary ve formátu čitelném pro OpenClaw."""
    print("\n" + "="*60)
    print(f"🌡️  POLYMARKET WEATHER BOT — DENNÍ NÁKUP")
    print(f"📅 Target date: {results['target_date']}")
    print(f"⏰ Spuštěno: {results['run_at']}")
    print("="*60)

    print(f"\n📊 PŘEDPOVĚDI POČASÍ ({results['forecasts_fetched']} měst):")
    for fc in forecasts:
        print(f"   {fc.city:12s} → {fc.predicted_high:5.1f}°{fc.unit}  [{fc.source}]")

    print(f"\n💰 OBCHODOVÁNÍ:")
    print(f"   Nalezeno trhů:     {results['markets_found']}")
    print(f"   Otevřeno pozic:    {results['positions_opened']}")
    print(f"   Přeskočeno:        {results['positions_skipped']}")

    if results["trades"]:
        print(f"\n📝 DETAILY OBCHODŮ:")
        for t in results["trades"]:
            icon = "✅" if t["action"] == "OPENED" else ("⏭️" if t["action"] == "SKIPPED" else "❌")
            price_str = f" @ {t.get('entry_price', 0):.3f}" if "entry_price" in t else ""
            reason_str = f" ({t.get('reason', '')})" if t["action"] != "OPENED" else ""
            print(f"   {icon} {t['city']:12s} {t.get('predicted_temp', '?'):.1f}°{t.get('unit','?')}{price_str}{reason_str}")

    print(f"\n💼 PORTFOLIO:")
    print(f"   Balance:           ${results['portfolio_balance']:.2f}")

    if results["errors"]:
        print(f"\n⚠️  CHYBY ({len(results['errors'])}):")
        for err in results["errors"]:
            print(f"   • {err}")

    print("="*60 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_daily_buy()

    # Výstup jako JSON pro případné zpracování OpenClaw agentem
    output_file = LOG_DIR / f"daily_buy_{date.today().isoformat()}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info("Výsledky uloženy do: %s", output_file)

    # Exit code
    sys.exit(0 if not result["errors"] else 1)
      
