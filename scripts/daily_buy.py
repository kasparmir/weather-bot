"""
daily_buy.py — Nákupní skript (hodinový, timezone-aware)
=========================================================
Spouštěn každou hodinu z run.py nebo cronu.

Logika výběru okna:
  - Pro každé město zjistíme aktuální lokální čas.
  - Nákup proběhne jen pokud jsme v okně:
      (BUY_HOURS_BEFORE) hodin před půlnocí → 0:00 místního času
    Příklad (BUY_HOURS_BEFORE=4): nákup probíhá v 20:00–23:59 lokálně.
  - Target date = zítřek v lokálním čase daného města.
  - Časová kontrola se provede PŘED voláním weather API → nezatěžujeme
    API pro města, která jsou mimo nákupní okno.
  - Duplikáty jsou blokovány ledgerem (city + target_date unique constraint).

Konfigurace:
  BUY_HOURS_BEFORE=4    # počet hodin před půlnocí (výchozí: 4)
"""

from __future__ import annotations
from typing import Optional
from zoneinfo import ZoneInfo

import json
import logging
import os
import sys
from datetime import date, timedelta, timezone, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from weather_api import WeatherCollector, WeatherForecast, CITY_MAP, CITIES, CityConfig
from polymarket_gamma import PolymarketGamma
from ledger import PaperLedger, TRADES_CSV, PORTFOLIO_JSON, BALANCE_HISTORY_CSV, DATA_DIR
from edge import check_edge, compute_edge, extract_market_info, MIN_EDGE

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
# Konfigurace
# ---------------------------------------------------------------------------
# Počet hodin před půlnocí místního času kdy se provádí nákup
# Výchozí: 4 → okno 20:00–23:59 lokálně
BUY_HOURS_BEFORE = int(os.getenv("BUY_HOURS_BEFORE", "4"))


# ---------------------------------------------------------------------------
# Timezone logika
# ---------------------------------------------------------------------------

def _city_local_now(city: CityConfig) -> datetime:
    """Aktuální čas v časovém pásmu daného města."""
    return datetime.now(ZoneInfo(city.timezone))


def _is_in_buy_window(city: CityConfig, now_utc: datetime) -> tuple[bool, date | None]:
    """
    Zkontroluje, zda jsme v nákupním okně pro dané město.

    Logika: "kup BUY_HOURS_BEFORE hodin před 00:00 cílového dne"
    Target = den jehož 00:00 (půlnoc) je do BUY_HOURS_BEFORE hodin od teď.

    Příklad (BUY_HOURS_BEFORE=4):
      22:30 lokálně → do půlnoci 1.5 h < 4 h → v okně, target = zítřek
      19:45 lokálně → do půlnoci 4.25 h > 4 h → mimo okno

    Výpočet v sekundách (přesný, nezávislý na DST).
    """
    from datetime import time as dtime
    local_now = now_utc.astimezone(ZoneInfo(city.timezone))

    # Přesný čas příští půlnoci v lokálním čase
    midnight = datetime.combine(
        local_now.date() + timedelta(days=1),
        dtime(0, 0, 0),
        tzinfo=ZoneInfo(city.timezone),
    )
    hours_until_midnight = (midnight - local_now).total_seconds() / 3600.0

    if hours_until_midnight <= BUY_HOURS_BEFORE:
        target = midnight.date()  # den jehož 00:00 nastane do BUY_HOURS_BEFORE hodin
        logger.debug(
            "%s: do půlnoci %.2f h <= %d h → v okně, target=%s",
            city.name, hours_until_midnight, BUY_HOURS_BEFORE, target,
        )
        return True, target

    logger.debug(
        "%s: do půlnoci %.2f h > %d h → mimo okno",
        city.name, hours_until_midnight, BUY_HOURS_BEFORE,
    )
    return False, None


# ---------------------------------------------------------------------------
# Hlavní funkce
# ---------------------------------------------------------------------------

def run_daily_buy() -> dict:
    """
    Projde všechna města, pro každé zkontroluje časové okno,
    a pokud je v okně a nemá ještě pozici, nakoupí.
    """
    now_utc = datetime.now(timezone.utc)
    logger.info("=== NÁKUPNÍ KONTROLA === %s UTC", now_utc.strftime("%Y-%m-%d %H:%M"))
    logger.info("BUY_HOURS_BEFORE=%d (nakupuje pokud do půlnoci <= %d h lokálně)",
                BUY_HOURS_BEFORE, BUY_HOURS_BEFORE)

    collector = WeatherCollector(meteoblue_api_key=os.getenv("METEOBLUE_API_KEY", ""))
    gamma = PolymarketGamma()
    ledger = PaperLedger()

    results = {
        "run_at": now_utc.isoformat(),
        "buy_hours_before": BUY_HOURS_BEFORE,
        "forecasts_fetched": 0,
        "markets_found": 0,
        "positions_opened": 0,
        "positions_skipped": 0,
        "cities_outside_window": 0,
        "errors": [],
        "trades": [],
        "portfolio_balance": ledger.portfolio.balance,
    }

    # Pro duplicate check načteme VŠECHNY trades (open i closed) pro dnešní target dates.
    # Důvod: po uzavření pozice (settlement/profit-take) nechceme hned rebuyovat
    # stejný market ve stejném nákupním cyklu.
    all_trades_today = {
        (t.city, t.target_date)
        for t in ledger.get_all_trades()
    }

    forecasts_for_summary: list[WeatherForecast] = []

    for city_cfg in CITIES:
        # --- 1. Časová kontrola (PŘED voláním API) ---
        in_window, target_date = _is_in_buy_window(city_cfg, now_utc)
        local_now = _city_local_now(city_cfg)

        if not in_window:
            logger.debug(
                "⏩ %s: mimo okno (lokálně %s, okno od %02d:00)",
                city_cfg.name, local_now.strftime("%H:%M"), 24 - BUY_HOURS_BEFORE,
            )
            results["cities_outside_window"] += 1
            continue

        # --- 2. Duplicate check (PŘED voláním API) ---
        if (city_cfg.name, target_date.isoformat()) in all_trades_today:
            logger.info(
                "⏭️  %s: pozice pro %s již existuje, přeskakuji",
                city_cfg.name, target_date,
            )
            results["positions_skipped"] += 1
            results["trades"].append({
                "city": city_cfg.name,
                "action": "SKIPPED",
                "reason": "duplicate",
                "target_date": target_date.isoformat(),
                "local_time": local_now.strftime("%H:%M %Z"),
            })
            continue

        logger.info(
            "✅ %s: v okně (lokálně %s) → target %s",
            city_cfg.name, local_now.strftime("%H:%M %Z"), target_date,
        )

        # --- 3. Forecast (teprve teď voláme API) ---
        try:
            forecast = collector.get_forecast(city_cfg.name, target_date)
        except Exception as exc:
            error_msg = f"Chyba předpovědi {city_cfg.name}: {exc}"
            logger.error(error_msg)
            results["errors"].append(error_msg)
            continue

        if not forecast:
            results["trades"].append({
                "city": city_cfg.name,
                "action": "NO_FORECAST",
                "target_date": target_date.isoformat(),
            })
            continue

        forecasts_for_summary.append(forecast)
        results["forecasts_fetched"] += 1

        # --- 4. Najdi trh a nakup ---
        try:
            trade_result = _process_forecast(forecast, gamma, ledger, target_date)
            if trade_result:
                results["trades"].append(trade_result)
                if trade_result["action"] == "OPENED":
                    results["markets_found"] += 1
                    results["positions_opened"] += 1
                elif trade_result["action"] == "SKIPPED":
                    results["positions_skipped"] += 1
        except Exception as exc:
            error_msg = f"Chyba obchodování {city_cfg.name}: {exc}"
            logger.error(error_msg, exc_info=True)
            results["errors"].append(error_msg)

    results["portfolio_balance"] = round(ledger.portfolio.balance, 2)
    _print_summary(results, forecasts_for_summary, now_utc)
    return results


def _process_forecast(
    forecast: WeatherForecast,
    gamma: PolymarketGamma,
    ledger: PaperLedger,
    target_date: date,
) -> dict | None:
    """Najde Polymarket kontrakt a otevře pozici."""
    city_cfg = CITY_MAP.get(forecast.city)
    polymarket_name = city_cfg.polymarket_name if city_cfg else forecast.city.lower().replace(" ", "-")

    logger.info(
        "Hledám trh: %s | předpověď %.1f°%s",
        forecast.city, forecast.predicted_high, forecast.unit,
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
        "  → Nalezen: %s | YES=%.4f NO=%.4f bestAsk=%.4f bestBid=%.4f lastTrade=%.4f",
        market.market_slug,
        market.yes_price,
        1.0 - market.yes_price,
        market.best_ask,
        market.best_bid,
        market.last_trade_price,
    )

    entry_price = market.yes_price
    # Horní limit 0.85: trhy nad 85% jsou téměř vyřešeny,
    # žádný smysluplný edge a hrozí koupě při settlement ceně
    if entry_price <= 0.05 or entry_price >= 0.85:
        reason = f"entry_price mimo rozsah (0.05–0.85): {entry_price:.4f}"
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

    # Edge filter
    threshold: Optional[float] = None
    direction: str = "unknown"
    threshold, direction = extract_market_info(market.market_slug, market.question, forecast.unit)

    if threshold is not None:
        edge_result = check_edge(forecast, threshold, direction, entry_price)
        if not edge_result.passes:
            return {
                "city": forecast.city,
                "action": "SKIPPED",
                "market_slug": market.market_slug,
                "predicted_temp": forecast.predicted_high,
                "unit": forecast.unit,
                "entry_price": entry_price,
                "our_probability": round(edge_result.our_probability, 3),
                "edge": round(edge_result.edge, 3),
                "reason": f"edge_filter: {edge_result.reason}",
            }
    else:
        logger.debug("  Edge: práh nelze určit pro %s", market.market_slug)

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

    edge_info: dict = {}
    if threshold is not None:
        er = compute_edge(forecast, threshold, direction, entry_price)
        edge_info = {"our_probability": round(er.our_probability, 3), "edge": round(er.edge, 3)}

    return {
        "city": forecast.city,
        "action": "OPENED",
        "trade_id": trade.id,
        "market_slug": market.market_slug,
        "predicted_temp": forecast.predicted_high,
        "unit": forecast.unit,
        "entry_price": entry_price,
        "yes_price_pct": market.yes_price_pct,
        **edge_info,
    }


# ---------------------------------------------------------------------------
# Výpis
# ---------------------------------------------------------------------------

def _print_summary(results: dict, forecasts: list[WeatherForecast],
                   now_utc: datetime) -> None:
    print("\n" + "="*62)
    print(f"🌡️  POLYMARKET WEATHER BOT — NÁKUPNÍ KONTROLA")
    print(f"⏰ UTC: {now_utc.strftime('%Y-%m-%d %H:%M')} | okno: posledních {results['buy_hours_before']}h před půlnocí")
    print("="*62)

    if results["cities_outside_window"]:
        print(f"\n⏩ Mimo okno: {results['cities_outside_window']} měst (API nevoláno)")

    if forecasts:
        print(f"\n📊 PŘEDPOVĚDI ({results['forecasts_fetched']} měst v okně):")
        for fc in forecasts:
            sigma_str = f" σ={fc.std_dev:.1f}°" if fc.std_dev > 0 else ""
            vals_str = ""
            if fc.ensemble_values:
                vals_str = " [" + ", ".join(
                    f"{s}={v:.0f}" for s, v in zip(fc.ensemble_sources, fc.ensemble_values)
                ) + "]"
            print(f"   {fc.city:12s} → {fc.predicted_high:5.1f}°{fc.unit}{sigma_str}  [{fc.source}]{vals_str}")

    print(f"\n💰 VÝSLEDEK:")
    print(f"   Otevřeno pozic:    {results['positions_opened']}")
    print(f"   Přeskočeno:        {results['positions_skipped']}")

    opened = [t for t in results["trades"] if t["action"] == "OPENED"]
    skipped = [t for t in results["trades"] if t["action"] == "SKIPPED"]
    no_market = [t for t in results["trades"] if t["action"] == "NO_MARKET"]

    if opened:
        print(f"\n📝 OTEVŘENÉ POZICE:")
        for t in opened:
            edge_str = f" (P={t.get('our_probability',0):.0%} edge={t.get('edge',0):+.0%})" if "edge" in t else ""
            print(f"   ✅ {t['city']:12s} {t.get('predicted_temp','?'):.1f}°{t.get('unit','?')}"
                  f" @ {t.get('entry_price',0):.3f}{edge_str}")

    if skipped:
        print(f"\n⏭️  PŘESKOČENO:")
        for t in skipped:
            print(f"   {t['city']:12s} {t.get('reason','')[:50]}")

    if no_market:
        print(f"\n❌ TRH NENALEZEN: {', '.join(t['city'] for t in no_market)}")

    print(f"\n💼 Balance: ${results['portfolio_balance']:.2f}")

    if results["errors"]:
        print(f"\n⚠️  CHYBY ({len(results['errors'])}):")
        for err in results["errors"]:
            print(f"   • {err}")

    print("="*62 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_daily_buy()
    output_file = LOG_DIR / f"daily_buy_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    logger.info("Výsledky uloženy: %s", output_file)
    sys.exit(0 if not result["errors"] else 1)