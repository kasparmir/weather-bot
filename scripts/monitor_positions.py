"""
monitor_positions.py — Monitor otevřených pozic
Spouštěn každých 30 minut přes OpenClaw cron.

Workflow:
  1. Načte všechny OPEN pozice z ledgeru
  2. Pro každou pozici získá aktuální cenu z Polymarketu
  3. Pokud cena >= 0.50 → prodej (profit take)
  4. Reportuje stav do logu (čitelné pro OpenClaw)
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

from polymarket_gamma import PolymarketGamma
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
        logging.FileHandler(LOG_DIR / "monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("monitor")

# Profit take — dvě podmínky (splnění JEDNÉ stačí pro prodej):
#   A) Absolutní tržní cena YES >= PROFIT_THRESHOLD (výchozí: 0.50)
#   B) Nerealizovaný P&L >= PROFIT_TAKE_ABS dolarů (výchozí: $0.50)
#      nebo >= PROFIT_TAKE_PCT procent (výchozí: 50%)
# Nastavení 0 = podmínka vypnuta
PROFIT_THRESHOLD  = float(os.getenv("PROFIT_TAKE_THRESHOLD",  "0.50"))  # YES cena
PROFIT_TAKE_ABS   = float(os.getenv("PROFIT_TAKE_ABS",        "0.50"))  # absolutní $ zisk
PROFIT_TAKE_PCT   = float(os.getenv("PROFIT_TAKE_PCT",        "0.50"))  # % zisk od entry
# Stop-loss: prodej pokud ztráta přesáhne X % z entry (default 70 %)
# Příklad: entry=0.40, stop_loss=0.70 → prodej pokud cena klesne pod 0.40*(1-0.70)=0.12
# Nastavení: STOP_LOSS_THRESHOLD=0.50 (50%), 0=vypnuto  (výchozí: 0.60 = 60%)
STOP_LOSS_THRESHOLD = float(os.getenv("STOP_LOSS_THRESHOLD", "0.60"))
# Minimální nárůst ceny pro zaznamenanou změnu (potlačení šumu)
PRICE_CHANGE_LOG_MIN = float(os.getenv("PRICE_CHANGE_LOG_MIN", "0.005"))


# ---------------------------------------------------------------------------
# Hlavní funkce
# ---------------------------------------------------------------------------

def run_monitor() -> dict:
    """
    Zkontroluje všechny otevřené pozice a vyřídí profit-take příkazy.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info("=== MONITOR POZIC === %s", now_iso)

    ledger = PaperLedger()
    gamma = PolymarketGamma()

    open_trades = ledger.get_open_trades()

    results = {
        "checked_at": now_iso,
        "open_positions": len(open_trades),
        "profit_takes": 0,
        "stop_losses": 0,
        "price_updates": 0,
        "errors": [],
        "positions": [],
        "portfolio_balance": ledger.portfolio.balance,
    }

    if not open_trades:
        logger.info("Žádné otevřené pozice k monitorování")
        _print_summary(results)
        return results

    logger.info("Monitoruji %d otevřených pozic...", len(open_trades))

    for trade in open_trades:
        pos_result = _check_position(trade, gamma, ledger)
        results["positions"].append(pos_result)

        if pos_result["action"] == "PROFIT_TAKE":
            results["profit_takes"] += 1
        elif pos_result["action"] == "STOP_LOSS":
            results["stop_losses"] += 1
        elif pos_result["action"] == "PRICE_UPDATED":
            results["price_updates"] += 1
        elif pos_result["action"] == "ERROR":
            results["errors"].append(pos_result.get("error", ""))

    results["portfolio_balance"] = round(ledger.portfolio.balance, 2)
    _print_summary(results)

    return results


def _check_position(trade: Trade, gamma: PolymarketGamma, ledger: PaperLedger) -> dict:
    """
    Zkontroluje jednu pozici.
    Vrátí dict s výsledkem akce.
    """
    logger.info(
        "Kontroluji: %s | %s | entry=%.4f | last=%.4f",
        trade.city, trade.market_slug, trade.entry_price, trade.current_price,
    )

    try:
        # Získej aktuální cenu z Gamma API
        market = gamma.get_market_price(trade.market_slug)

        if not market:
            error_msg = f"Trh nenalezen: {trade.market_slug}"
            logger.warning("  → %s", error_msg)
            return {
                "trade_id": trade.id,
                "city": trade.city,
                "action": "ERROR",
                "error": error_msg,
                "current_price": trade.current_price,
            }

        # Kontrola: trh uzavřen → settlement
        if market.closed or not market.active:
            yes_price = market.yes_price

            # Čekej na skutečnou výplatu: Polymarket resolvuje na 1.0 (YES vyhrál)
            # nebo 0.0 (NO vyhrál). Pokud je cena stále v šedé zóně (0.05–0.95),
            # trh ještě nebyl definitivně vyřešen — počkej na příští kontrolu.
            if 0.05 < yes_price < 0.95:
                logger.info(
                    "  ⏳ Trh uzavřen ale čeká na resolution: yes_price=%.4f (šedá zóna)",
                    yes_price,
                )
                # Aktualizuj cenu ale nuzavírej
                ledger.update_position_price(trade.id, yes_price)
                return {
                    "trade_id": trade.id,
                    "city": trade.city,
                    "action": "PRICE_UPDATED",
                    "prev_price": trade.current_price,
                    "current_price": yes_price,
                    "distance_to_target": round(PROFIT_THRESHOLD - yes_price, 4),
                    "forecast_diverged": trade.forecast_diverged,
                    "note": "awaiting_resolution",
                }

            # Cena je blízko 0 nebo 1 → definitivní výsledek
            logger.info(
                "  → Trh vyřešen: yes_price=%.4f (%s) | settlement",
                yes_price, "YES" if yes_price >= 0.95 else "NO",
            )
            settled_trade = ledger.close_position(
                trade_id=trade.id,
                exit_price=yes_price,
                reason="CLOSED_SETTLEMENT",
                notes=f"settlement: {'YES' if yes_price >= 0.95 else 'NO'} @ {yes_price:.4f}",
            )
            return {
                "trade_id": trade.id,
                "city": trade.city,
                "action": "SETTLEMENT",
                "exit_price": yes_price,
                "result": "YES" if yes_price >= 0.95 else "NO",
                "pnl": settled_trade.pnl if settled_trade else 0,
            }

        current_price = market.yes_price
        price_change = abs(current_price - trade.current_price)

        # Zkontroluj profit-take podmínku.
        #
        # Dvě varianty podle stavu předpovědi:
        #
        # A) Normální pozice (forecast_diverged=False):
        #    Prodej pokud cena >= 0.50 A >= entry+5 %.
        #
        # B) Diverged pozice (forecast_diverged=True):
        #    Předpověď se změnila od vstupu. Přímý exit je rizikový
        #    (trh mohl správně ocenit nový forecast). Proto počkáme
        #    na P&L zisk >= PROFIT_TAKE_PCT (výchozí 50 %) a pak prodáme.
        #    Exit cena = entry_price * (1 + PROFIT_TAKE_PCT).

        diverged_pct = float(os.getenv("DIVERGED_PROFIT_TAKE_PCT", "0.50"))

        if trade.forecast_diverged:
            # Čekáme na P&L zisk (ne absolutní cenu)
            min_exit = trade.entry_price * (1.0 + diverged_pct)
            if current_price >= min_exit:
                pnl_pct_actual = (current_price / trade.entry_price - 1) * 100
                logger.info(
                    "  🎯 PROFIT TAKE [DIVERGED]: %.4f >= %.4f (entry=%.4f +%.0f%%) | P&L=+%.1f%%",
                    current_price, min_exit, trade.entry_price,
                    diverged_pct * 100, pnl_pct_actual,
                )
                closed_trade = ledger.close_position(
                    trade_id=trade.id,
                    exit_price=current_price,
                    reason="CLOSED_PROFIT",
                    notes=(
                        f"profit-take [forecast_diverged]: {current_price:.4f} >= {min_exit:.4f} "
                        f"(entry={trade.entry_price:.4f}, entry_forecast={trade.predicted_temp:.1f}°{trade.unit}, "
                        f"latest_forecast={trade.latest_forecast_temp:.1f}°{trade.unit})"
                    ),
                )
                return {
                    "trade_id": trade.id,
                    "city": trade.city,
                    "action": "PROFIT_TAKE",
                    "mode": "diverged",
                    "entry_price": trade.entry_price,
                    "exit_price": current_price,
                    "pnl": closed_trade.pnl if closed_trade else 0,
                    "pnl_pct": closed_trade.pnl_pct if closed_trade else 0,
                    "entry_forecast": trade.predicted_temp,
                    "latest_forecast": trade.latest_forecast_temp,
                }
            else:
                remaining = min_exit - current_price
                logger.info(
                    "  🔶 DIVERGED: cena=%.4f, čeká na %.4f (+%.0f%% od entry) | zbývá +%.4f",
                    current_price, min_exit, diverged_pct * 100, remaining,
                )
        else:
            # Normální podmínky — splnění JEDNÉ stačí:
            #   A) YES cena >= PROFIT_THRESHOLD (výchozí 0.50) A >= entry+20%
            #      (20% ochrana: při entry=0.40 prodej jen pokud cena >= 0.50 AND >= 0.48)
            #   B) Nerealizovaný P&L >= PROFIT_TAKE_ABS dolarů
            #   C) Nerealizovaný P&L >= PROFIT_TAKE_PCT procent

            from ledger import POSITION_SIZE as _POSITION_SIZE
            unrealized_pnl_dollar = _POSITION_SIZE * (current_price / trade.entry_price - 1)
            unrealized_pnl_pct    = (current_price / trade.entry_price - 1) * 100

            # Podmínka A: cena >= PROFIT_THRESHOLD A zároveň >= entry + 20 %
            # Ochrana: zabrání prodeji hned jak cena přesáhne 0.50 při entry 0.48
            price_condition = (
                PROFIT_THRESHOLD > 0
                and current_price >= PROFIT_THRESHOLD
                and current_price >= trade.entry_price * 1.20
            )
            # Podmínka B: absolutní $ zisk
            abs_condition = (
                PROFIT_TAKE_ABS > 0
                and unrealized_pnl_dollar >= PROFIT_TAKE_ABS
            )
            # Podmínka C: procentuální zisk
            pct_condition = (
                PROFIT_TAKE_PCT > 0
                and unrealized_pnl_pct >= PROFIT_TAKE_PCT * 100
            )

            if price_condition or abs_condition or pct_condition:
                trigger = (
                    f"price≥{PROFIT_THRESHOLD}" if price_condition else
                    f"abs_pnl≥${PROFIT_TAKE_ABS:.2f}" if abs_condition else
                    f"pnl_pct≥{PROFIT_TAKE_PCT:.0%}"
                )
                logger.info(
                    "  🎯 PROFIT TAKE [%s]: cena=%.4f | P&L=$%.2f (+%.1f%%)",
                    trigger, current_price, unrealized_pnl_dollar, unrealized_pnl_pct,
                )
                closed_trade = ledger.close_position(
                    trade_id=trade.id,
                    exit_price=current_price,
                    reason="CLOSED_PROFIT",
                    notes=(
                        f"profit-take [{trigger}]: cena={current_price:.4f} "
                        f"pnl=${unrealized_pnl_dollar:.2f} ({unrealized_pnl_pct:.1f}%) "
                        f"entry={trade.entry_price:.4f}"
                    ),
                )
                return {
                    "trade_id": trade.id,
                    "city": trade.city,
                    "action": "PROFIT_TAKE",
                    "mode": f"normal[{trigger}]",
                    "entry_price": trade.entry_price,
                    "exit_price": current_price,
                    "pnl": closed_trade.pnl if closed_trade else 0,
                    "pnl_pct": closed_trade.pnl_pct if closed_trade else 0,
                }

        # Zkontroluj stop-loss podmínku
        if STOP_LOSS_THRESHOLD > 0:
            stop_price = trade.entry_price * (1.0 - STOP_LOSS_THRESHOLD)
            if current_price <= stop_price:
                loss_pct = (1.0 - current_price / trade.entry_price) * 100
                logger.info(
                    "  🛑 STOP-LOSS: %.4f <= %.4f (entry=%.4f -%.0f%%) | Prodávám",
                    current_price, stop_price, trade.entry_price, loss_pct,
                )
                closed_trade = ledger.close_position(
                    trade_id=trade.id,
                    exit_price=current_price,
                    reason="CLOSED_STOP_LOSS",
                    notes=f"stop-loss: {current_price:.4f} <= {stop_price:.4f} (entry={trade.entry_price:.4f}, limit={STOP_LOSS_THRESHOLD:.0%})",
                )
                return {
                    "trade_id": trade.id,
                    "city": trade.city,
                    "action": "STOP_LOSS",
                    "entry_price": trade.entry_price,
                    "exit_price": current_price,
                    "stop_price": stop_price,
                    "pnl": closed_trade.pnl if closed_trade else 0,
                    "pnl_pct": closed_trade.pnl_pct if closed_trade else 0,
                }

        # Jinak: aktualizuj cenu
        if price_change >= PRICE_CHANGE_LOG_MIN:
            logger.info(
                "  📈 Cena: %.4f → %.4f (Δ%+.4f) | profit=%.2f stop=%.4f",
                trade.current_price, current_price,
                current_price - trade.current_price,
                PROFIT_THRESHOLD,
                trade.entry_price * (1.0 - STOP_LOSS_THRESHOLD) if STOP_LOSS_THRESHOLD > 0 else 0,
            )
        else:
            logger.debug("  ↔ Cena beze změny: %.4f", current_price)

        ledger.update_position_price(trade.id, current_price)

        return {
            "trade_id": trade.id,
            "city": trade.city,
            "action": "PRICE_UPDATED",
            "prev_price": trade.current_price,
            "current_price": current_price,
            "distance_to_target": round(PROFIT_THRESHOLD - current_price, 4),
            "stop_price": round(trade.entry_price * (1.0 - STOP_LOSS_THRESHOLD), 4) if STOP_LOSS_THRESHOLD > 0 else None,
            "forecast_diverged": trade.forecast_diverged,
        }

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("  ❌ Chyba při kontrole %s: %s", trade.city, error_msg, exc_info=True)
        return {
            "trade_id": trade.id,
            "city": trade.city,
            "action": "ERROR",
            "error": error_msg,
            "current_price": trade.current_price,
        }


def _print_summary(results: dict) -> None:
    """OpenClaw-friendly výstup."""
    print("\n" + "-"*50)
    print(f"📡 MONITOR POZIC — {results['checked_at'][:19]}Z")
    print("-"*50)
    print(f"   Otevřených pozic:  {results['open_positions']}")
    print(f"   Profit takes:      {results['profit_takes']}")
    print(f"   Stop-losses:       {results.get('stop_losses', 0)}")
    print(f"   Aktualizací cen:   {results['price_updates']}")
    print(f"   Balance:           ${results['portfolio_balance']:.2f}")

    if results["positions"]:
        print(f"\n   POZICE:")
        for pos in results["positions"]:
            action = pos["action"]
            icon = {
                "PROFIT_TAKE":   "🎯",
                "STOP_LOSS":     "🛑",
                "SETTLEMENT":    "📋",
                "PRICE_UPDATED": "📊",
                "ERROR":         "❌",
            }.get(action, "❔")

            if action == "PROFIT_TAKE":
                print(
                    f"   {icon} {pos['city']:12s} PRODEJ @ {pos['exit_price']:.4f} "
                    f"| P&L: ${pos.get('pnl', 0):+.2f} ({pos.get('pnl_pct', 0):+.1f}%)"
                )
            elif action == "STOP_LOSS":
                print(
                    f"   {icon} {pos['city']:12s} STOP-LOSS @ {pos['exit_price']:.4f} "
                    f"(limit {pos.get('stop_price',0):.4f}) "
                    f"| P&L: ${pos.get('pnl', 0):+.2f} ({pos.get('pnl_pct', 0):+.1f}%)"
                )
            elif action == "PRICE_UPDATED":
                dist = pos.get("distance_to_target", "?")
                div_flag = " [⚠️DIVERGED]" if pos.get("forecast_diverged") else ""
                stop_str = f" stop={pos['stop_price']:.4f}" if pos.get("stop_price") else ""
                print(
                    f"   {icon} {pos['city']:12s} {pos.get('current_price', 0):.4f} "
                    f"(do targetu: {dist:.4f}){stop_str}{div_flag}"
                )
            elif action == "SETTLEMENT":
                print(
                    f"   {icon} {pos['city']:12s} SETTLEMENT @ {pos.get('exit_price', 0):.4f} "
                    f"| P&L: ${pos.get('pnl', 0):+.2f}"
                )
            elif action == "ERROR":
                print(f"   {icon} {pos['city']:12s} {pos.get('error', '')}")

    if results["errors"]:
        print(f"\n   ⚠️  CHYBY:")
        for e in results["errors"]:
            if e:
                print(f"      • {e}")

    print("-"*50)

    # Tichý výstup pokud není co hlásit (pro OpenClaw HEARTBEAT_OK)
    if results["open_positions"] == 0 and results["profit_takes"] == 0 and results.get("stop_losses", 0) == 0:
        print("HEARTBEAT_OK — žádné akce potřeba")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = run_monitor()

    output_file = LOG_DIR / f"monitor_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    sys.exit(0 if not result["errors"] else 1)