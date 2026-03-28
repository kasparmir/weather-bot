"""
ledger.py — Paper Trading Ledger
CSV záznam obchodů + JSON portfolio stav.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konstanty
# ---------------------------------------------------------------------------

INITIAL_BALANCE = 100.0           # $100 USDC virtuální balance
POSITION_SIZE = 4.0               # $ na jednu pozici
PROFIT_TAKE_THRESHOLD = 0.50      # 50% → prodej (cena >= 0.50)

# Cesty k datovým souborům (lze přepsat env proměnnými)
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", Path(__file__).parent.parent / "data"))
TRADES_CSV = DATA_DIR / "trades.csv"
PORTFOLIO_JSON = DATA_DIR / "portfolio.json"
BALANCE_HISTORY_CSV = DATA_DIR / "balance_history.csv"

CSV_FIELDNAMES = [
    "id",
    "timestamp",
    "city",
    "target_date",
    "predicted_temp",
    "unit",
    "market_slug",
    "market_question",
    "entry_price",
    "entry_timestamp",
    "current_price",
    "last_checked",
    "status",          # OPEN | CLOSED_PROFIT | CLOSED_SETTLEMENT | CLOSED_STOP_LOSS | CLOSED_MANUAL
    "exit_price",
    "exit_timestamp",
    "pnl",             # profit/loss v $
    "pnl_pct",         # profit/loss v %
    "notes",
    "forecast_diverged",     # "1" pokud aktuální předpověď nesedí s entry
    "latest_forecast_temp",  # poslední předpověď (může se lišit od predicted_temp)
    "forecast_checked_at",   # kdy byl naposledy proveden recheck předpovědi
]

TradeStatus = Literal[
    "OPEN",
    "CLOSED_PROFIT",
    "CLOSED_SETTLEMENT",
    "CLOSED_STOP_LOSS",
    "CLOSED_MANUAL",
]


# ---------------------------------------------------------------------------
# Datové třídy
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    id: str
    timestamp: str            # ISO UTC — kdy byl záznam vytvořen
    city: str
    target_date: str          # ISO date
    predicted_temp: float
    unit: str                 # F / C
    market_slug: str
    market_question: str
    entry_price: float        # 0.0–1.0
    entry_timestamp: str      # ISO UTC
    current_price: float = 0.0
    last_checked: str = ""
    status: TradeStatus = "OPEN"
    exit_price: float = 0.0
    exit_timestamp: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    notes: str = ""
    forecast_diverged: bool = False          # True = aktuální forecast se liší od entry
    latest_forecast_temp: float = 0.0       # poslední re-check forecast hodnota
    forecast_checked_at: str = ""           # ISO UTC posledního rechecku

    def to_dict(self) -> dict:
        d = asdict(self)
        # Serializuj bool jako "1"/"0" pro CSV kompatibilitu
        d["forecast_diverged"] = "1" if self.forecast_diverged else "0"
        return d


@dataclass
class Portfolio:
    balance: float = INITIAL_BALANCE
    total_invested: float = 0.0
    open_positions_count: int = 0
    closed_positions_count: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_updated: str = ""

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0

    @property
    def current_equity(self) -> float:
        """Aktuální hodnota portfolia (balance + investované)."""
        return self.balance + self.total_invested

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "win_rate": round(self.win_rate, 1),
            "current_equity": round(self.current_equity, 2),
            "initial_balance": INITIAL_BALANCE,
            "total_return_pct": round(
                (self.current_equity - INITIAL_BALANCE) / INITIAL_BALANCE * 100, 2
            ),
        }


# ---------------------------------------------------------------------------
# Hlavní třída
# ---------------------------------------------------------------------------

class PaperLedger:
    """
    Paper trading ledger pro Polymarket Weather Bot.

    Spravuje:
    - CSV soubor s historií obchodů
    - JSON soubor se stavem portfolia
    - CSV soubor s historií balance
    """

    def __init__(
        self,
        trades_csv: Path = TRADES_CSV,
        portfolio_json: Path = PORTFOLIO_JSON,
        balance_history_csv: Path = BALANCE_HISTORY_CSV,
        position_size: float = POSITION_SIZE,
        profit_take_threshold: float = PROFIT_TAKE_THRESHOLD,
    ):
        self.trades_csv = trades_csv
        self.portfolio_json = portfolio_json
        self.balance_history_csv = balance_history_csv
        self.position_size = position_size
        self.profit_take_threshold = profit_take_threshold

        # Inicializace souborů
        self.trades_csv.parent.mkdir(parents=True, exist_ok=True)
        self._init_csv()
        self.portfolio = self._load_portfolio()   # načíst PŘED _init_balance_history
        self._init_balance_history()

    # ------------------------------------------------------------------
    # Veřejné metody — obchodování
    # ------------------------------------------------------------------

    def open_position(
        self,
        city: str,
        target_date: date,
        predicted_temp: float,
        unit: str,
        market_slug: str,
        market_question: str,
        entry_price: float,
    ) -> Optional[Trade]:
        """
        Otevře novou papírovou pozici.
        Vrátí None, pokud není dostatek balance nebo pozice již existuje.
        """
        # Kontrola duplicit
        open_trades = self.get_open_trades()
        for t in open_trades:
            if t.city == city and t.target_date == target_date.isoformat():
                logger.info("Pozice pro %s dne %s již existuje (%s)", city, target_date, t.id)
                return None

        # Kontrola balance
        if self.portfolio.balance < self.position_size:
            logger.warning("Nedostatečná balance: $%.2f < $%.2f", self.portfolio.balance, self.position_size)
            return None

        if entry_price <= 0 or entry_price >= 1:
            logger.warning("Neplatná entry_price: %.4f (musí být 0–1)", entry_price)
            return None

        now_iso = _now_iso()
        trade_id = _generate_id()

        trade = Trade(
            id=trade_id,
            timestamp=now_iso,
            city=city,
            target_date=target_date.isoformat(),
            predicted_temp=predicted_temp,
            unit=unit,
            market_slug=market_slug,
            market_question=market_question,
            entry_price=entry_price,
            entry_timestamp=now_iso,
            current_price=entry_price,
            last_checked=now_iso,
            status="OPEN",
        )

        # Aktualizace portfolia
        self.portfolio.balance -= self.position_size
        self.portfolio.total_invested += self.position_size
        self.portfolio.open_positions_count += 1
        self.portfolio.last_updated = now_iso

        # Uložení
        self._append_trade(trade)
        self._save_portfolio()

        logger.info(
            "✅ KOUPIT %s @ %.4f | %s dne %s | ID: %s",
            city, entry_price, market_slug, target_date, trade_id,
        )
        return trade

    def update_position_price(self, trade_id: str, current_price: float) -> Optional[Trade]:
        """
        Aktualizuje aktuální cenu otevřené pozice.
        Pokud cena překročí threshold → automaticky zavře pozici (profit take).
        """
        trade = self._find_trade(trade_id)
        if not trade or trade.status != "OPEN":
            return None

        trade.current_price = current_price
        trade.last_checked = _now_iso()

        # Kontrola profit-take podmínky
        if current_price >= self.profit_take_threshold:
            return self.close_position(trade_id, current_price, reason="CLOSED_PROFIT")

        self._update_trade_row(trade)
        return trade

    def close_position(
        self,
        trade_id: str,
        exit_price: float,
        reason: TradeStatus = "CLOSED_SETTLEMENT",
        notes: str = "",
    ) -> Optional[Trade]:
        """
        Zavře pozici a spočítá P&L.
        """
        trade = self._find_trade(trade_id)
        if not trade or trade.status != "OPEN":
            logger.warning("Nelze zavřít obchod %s: status=%s", trade_id,
                           trade.status if trade else "nenalezen")
            return None

        now_iso = _now_iso()

        # P&L kalkulace
        # Vstupní cena: platíme `entry_price` za kontrakt v hodnotě $1 při win
        # Výstupní hodnota: `exit_price` × position_size / entry_price × exit_price... 
        # Zjednodušený model: investice = position_size, výnos = position_size * (exit_price / entry_price)
        pnl_dollar = self.position_size * (exit_price / trade.entry_price - 1)
        pnl_pct = (exit_price / trade.entry_price - 1) * 100

        trade.status = reason
        trade.exit_price = exit_price
        trade.exit_timestamp = now_iso
        trade.current_price = exit_price
        trade.pnl = round(pnl_dollar, 4)
        trade.pnl_pct = round(pnl_pct, 2)
        trade.notes = notes

        # Aktualizace portfolia
        returned_capital = self.position_size + pnl_dollar
        self.portfolio.balance += returned_capital
        self.portfolio.total_invested -= self.position_size
        self.portfolio.open_positions_count = max(0, self.portfolio.open_positions_count - 1)
        self.portfolio.closed_positions_count += 1
        self.portfolio.total_pnl += pnl_dollar
        if pnl_dollar >= 0:
            self.portfolio.wins += 1
        else:
            self.portfolio.losses += 1
        self.portfolio.last_updated = now_iso

        self._update_trade_row(trade)
        self._save_portfolio()
        self._record_balance()

        logger.info(
            "📊 %s %s @ %.4f → exit %.4f | P&L: $%.2f (%.1f%%) | Balance: $%.2f",
            reason, trade.city, trade.entry_price, exit_price,
            pnl_dollar, pnl_pct, self.portfolio.balance,
        )
        return trade

    # ------------------------------------------------------------------
    # Dotazovací metody
    # ------------------------------------------------------------------

    def get_open_trades(self) -> list[Trade]:
        """Vrátí všechny otevřené pozice."""
        return [t for t in self._read_all_trades() if t.status == "OPEN"]

    def get_all_trades(self) -> list[Trade]:
        """Vrátí všechny obchody (seřazené od nejnovějšího)."""
        trades = self._read_all_trades()
        trades.sort(key=lambda t: t.timestamp, reverse=True)
        return trades

    def get_closed_trades(self) -> list[Trade]:
        """Vrátí všechny zavřené obchody."""
        return [t for t in self.get_all_trades() if t.status != "OPEN"]

    def get_balance_history(self) -> list[dict]:
        """Vrátí historii balance pro grafy."""
        if not self.balance_history_csv.exists():
            return []
        rows = []
        with open(self.balance_history_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        return rows

    def get_stats(self) -> dict:
        """Souhrnné statistiky portfolia."""
        closed = self.get_closed_trades()
        profit_trades = [t for t in closed if t.pnl > 0]
        loss_trades = [t for t in closed if t.pnl <= 0]

        avg_pnl = sum(t.pnl for t in closed) / len(closed) if closed else 0
        avg_win = sum(t.pnl for t in profit_trades) / len(profit_trades) if profit_trades else 0
        avg_loss = sum(t.pnl for t in loss_trades) / len(loss_trades) if loss_trades else 0

        return {
            **self.portfolio.to_dict(),
            "closed_count": len(closed),
            "open_count": len(self.get_open_trades()),
            "avg_pnl_per_trade": round(avg_pnl, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
        }

    # ------------------------------------------------------------------
    # Interní I/O metody
    # ------------------------------------------------------------------

    def _init_csv(self) -> None:
        if not self.trades_csv.exists():
            with open(self.trades_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                writer.writeheader()

    def _init_balance_history(self) -> None:
        if not self.balance_history_csv.exists():
            with open(self.balance_history_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["timestamp", "balance", "equity", "event"])
                writer.writeheader()
            # Zaznamenat počáteční stav
            self._record_balance(event="INIT")

    def _append_trade(self, trade: Trade) -> None:
        with open(self.trades_csv, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            row = trade.to_dict()
            writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})

    def _update_trade_row(self, updated_trade: Trade) -> None:
        """Přepíše řádek v CSV (kompletní rewrite souboru)."""
        trades = self._read_all_trades()
        updated = False
        for i, t in enumerate(trades):
            if t.id == updated_trade.id:
                trades[i] = updated_trade
                updated = True
                break

        if not updated:
            logger.error("_update_trade_row: trade ID %s nenalezen", updated_trade.id)
            return

        with open(self.trades_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            for t in trades:
                row = t.to_dict()
                writer.writerow({k: row.get(k, "") for k in CSV_FIELDNAMES})

    def _read_all_trades(self) -> list[Trade]:
        if not self.trades_csv.exists():
            return []
        trades = []
        with open(self.trades_csv, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    trade = Trade(
                        id=row["id"],
                        timestamp=row["timestamp"],
                        city=row["city"],
                        target_date=row["target_date"],
                        predicted_temp=float(row.get("predicted_temp") or 0),
                        unit=row.get("unit", "F"),
                        market_slug=row["market_slug"],
                        market_question=row.get("market_question", ""),
                        entry_price=float(row.get("entry_price") or 0),
                        entry_timestamp=row.get("entry_timestamp", ""),
                        current_price=float(row.get("current_price") or 0),
                        last_checked=row.get("last_checked", ""),
                        status=row.get("status", "OPEN"),  # type: ignore
                        exit_price=float(row.get("exit_price") or 0),
                        exit_timestamp=row.get("exit_timestamp", ""),
                        pnl=float(row.get("pnl") or 0),
                        pnl_pct=float(row.get("pnl_pct") or 0),
                        notes=row.get("notes", ""),
                        forecast_diverged=row.get("forecast_diverged", "").lower() in ("1", "true"),
                        latest_forecast_temp=float(row.get("latest_forecast_temp") or 0),
                        forecast_checked_at=row.get("forecast_checked_at", ""),
                    )
                    trades.append(trade)
                except Exception as exc:
                    logger.error("Chyba čtení řádku CSV: %s | %s", exc, row)
        return trades

    def mark_forecast_diverged(
        self,
        trade_id: str,
        latest_forecast_temp: float,
        diverged: bool = True,
    ) -> Optional[Trade]:
        """
        Označí pozici jako forecast_diverged.
        Ukládá aktuální předpověď a timestamp rechecku.
        """
        trade = self._find_trade(trade_id)
        if not trade or trade.status != "OPEN":
            return None
        trade.forecast_diverged = diverged
        trade.latest_forecast_temp = latest_forecast_temp
        trade.forecast_checked_at = _now_iso()
        self._update_trade_row(trade)
        logger.info(
            "📡 Forecast recheck %s (%s): entry=%.1f°%s → now=%.1f°%s | diverged=%s",
            trade.city, trade_id,
            trade.predicted_temp, trade.unit,
            latest_forecast_temp, trade.unit,
            diverged,
        )
        return trade

    def _find_trade(self, trade_id: str) -> Optional[Trade]:
        for t in self._read_all_trades():
            if t.id == trade_id:
                return t
        return None

    def _load_portfolio(self) -> Portfolio:
        if not self.portfolio_json.exists():
            p = Portfolio(last_updated=_now_iso())
            self._save_portfolio(p)
            return p
        try:
            with open(self.portfolio_json, encoding="utf-8") as f:
                data = json.load(f)
            return Portfolio(
                balance=float(data.get("balance", INITIAL_BALANCE)),
                total_invested=float(data.get("total_invested", 0)),
                open_positions_count=int(data.get("open_positions_count", 0)),
                closed_positions_count=int(data.get("closed_positions_count", 0)),
                total_pnl=float(data.get("total_pnl", 0)),
                wins=int(data.get("wins", 0)),
                losses=int(data.get("losses", 0)),
                last_updated=data.get("last_updated", _now_iso()),
            )
        except Exception as exc:
            logger.error("Chyba načítání portfolia: %s — resetuji na výchozí", exc)
            return Portfolio(last_updated=_now_iso())

    def _save_portfolio(self, portfolio: Portfolio | None = None) -> None:
        p = portfolio or self.portfolio
        with open(self.portfolio_json, "w", encoding="utf-8") as f:
            json.dump(p.to_dict(), f, indent=2, ensure_ascii=False)

    def _record_balance(self, event: str = "") -> None:
        """Zaznamená snapshot balance do balance_history.csv."""
        row = {
            "timestamp": _now_iso(),
            "balance": round(self.portfolio.balance, 4),
            "equity": round(self.portfolio.current_equity, 4),
            "event": event,
        }
        with open(self.balance_history_csv, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["timestamp", "balance", "equity", "event"])
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


# ---------------------------------------------------------------------------
# Testovací spuštění
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with tempfile.TemporaryDirectory() as tmpdir:
        import os
        os.environ["BOT_DATA_DIR"] = tmpdir

        # Inicializuj ledger s explicitními cestami (přepíše modulo-level konstanty)
        _tmp = Path(tmpdir)
        ledger = PaperLedger(
            trades_csv=_tmp / "trades.csv",
            portfolio_json=_tmp / "portfolio.json",
            balance_history_csv=_tmp / "balance_history.csv",
        )

        print(f"\n=== Počáteční stav ===")
        print(f"Balance: ${ledger.portfolio.balance:.2f}")

        # Test: otevření pozice
        trade = ledger.open_position(
            city="New York",
            target_date=date.today(),
            predicted_temp=72.0,
            unit="F",
            market_slug="highest-temperature-in-new-york-on-march-25-2026",
            market_question="Will the high temperature in New York exceed 72°F?",
            entry_price=0.35,
        )
        print(f"\nOtevřená pozice: {trade.id if trade else 'SELHALO'}")
        print(f"Balance po nákupu: ${ledger.portfolio.balance:.2f}")

        # Simulace růstu ceny
        if trade:
            updated = ledger.update_position_price(trade.id, 0.62)
            print(f"\nCena vzrostla na 0.62 → profit take: {updated.status if updated else 'žádná akce'}")
            print(f"Balance po prodeji: ${ledger.portfolio.balance:.2f}")

        print(f"\nStatistiky: {json.dumps(ledger.get_stats(), indent=2)}")