"""
reset_portfolio.py — Reset papírového portfolia
Vymaže všechna data a začne znovu s $1000 USDC.

Použití:
    python scripts/reset_portfolio.py [--confirm]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

DATA_DIR = Path(os.getenv("BOT_DATA_DIR", Path(__file__).parent.parent / "data"))
TRADES_CSV = DATA_DIR / "trades.csv"
PORTFOLIO_JSON = DATA_DIR / "portfolio.json"
BALANCE_HISTORY_CSV = DATA_DIR / "balance_history.csv"


def reset(confirm: bool = False) -> None:
    if not confirm:
        print("⚠️  VAROVÁNÍ: Toto vymaže VŠECHNA data portfolia!")
        print(f"   Soubory k smazání:")
        print(f"   - {TRADES_CSV}")
        print(f"   - {PORTFOLIO_JSON}")
        print(f"   - {BALANCE_HISTORY_CSV}")
        print()
        answer = input("Pokračovat? [yes/no]: ").strip().lower()
        if answer not in ("yes", "y"):
            print("Reset zrušen.")
            return

    # Záloha starých dat
    backup_dir = DATA_DIR / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for f in [TRADES_CSV, PORTFOLIO_JSON, BALANCE_HISTORY_CSV]:
        if f.exists():
            import shutil
            shutil.copy2(f, backup_dir / f.name)
            f.unlink()
            print(f"✓ Zálohováno a smazáno: {f.name}")

    print(f"\n📁 Záloha uložena v: {backup_dir}")

    # Reinicializace přes PaperLedger
    from ledger import PaperLedger
    ledger = PaperLedger(
        trades_csv=TRADES_CSV,
        portfolio_json=PORTFOLIO_JSON,
        balance_history_csv=BALANCE_HISTORY_CSV,
    )
    print(f"\n✅ Portfolio resetováno: balance = ${ledger.portfolio.balance:.2f} USDC")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset paper trading portfolia")
    parser.add_argument("--confirm", action="store_true", help="Přeskoč potvrzovací dialog")
    args = parser.parse_args()
    reset(confirm=args.confirm)
