"""
polymarket_gamma.py — Polymarket Gamma API klient
Hledá weather kontrakty a získává aktuální ceny.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# ---------------------------------------------------------------------------
# Datové třídy
# ---------------------------------------------------------------------------

@dataclass
class MarketOutcome:
    name: str       # "Yes" nebo "No"
    token_id: str
    price: float    # 0.0 – 1.0

@dataclass
class WeatherMarket:
    market_id: str
    slug: str
    question: str
    end_date: str
    last_trade_price: float    # 0.0 – 1.0
    best_ask: float            # 0.0 – 1.0
    best_bid: float            # 0.0 – 1.0
    active: bool
    closed: bool
    outcomes: list[MarketOutcome] = field(default_factory=list)

    @property
    def yes_price(self) -> float:
        """Cena YES kontraktu (0.0–1.0)."""
        for o in self.outcomes:
            if o.name.lower() == "yes":
                return o.price
        return self.last_trade_price

    @property
    def yes_price_pct(self) -> float:
        """Cena YES kontraktu v procentech (0–100)."""
        return round(self.yes_price * 100, 2)


# ---------------------------------------------------------------------------
# Klient
# ---------------------------------------------------------------------------

class PolymarketGamma:
    """
    Klient pro Polymarket Gamma API.
    Specializovaný na hledání weather/temperature trhů.
    """

    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Hlavní metody
    # ------------------------------------------------------------------

    def find_weather_market(
        self,
        city_polymarket_name: str,
        target_date: date,
        predicted_temp: float,
        unit: str,
    ) -> Optional[WeatherMarket]:
        """
        Najde nejrelevantnější weather market pro dané město a datum.

        Strategie (od nejpřesnějšího):
          1. Hledání podle slug (exact match s předpovídanou teplotou)
          2. Fulltextové hledání podle města + data
          3. Fallback: nejaktivnější weather market pro město
        """
        # Strategie 1: Přesný slug podle předpovídané teploty
        slug = self._build_slug(city_polymarket_name, target_date, predicted_temp, unit)
        market = self._fetch_by_slug(slug)
        if market and market.active and not market.closed:
            logger.info("✓ Nalezen trh přes slug: %s", slug)
            return market

        # Strategie 2: Fulltextové hledání
        market = self._search_weather_market(city_polymarket_name, target_date)
        if market:
            logger.info("✓ Nalezen trh přes search: %s", market.slug)
            return market

        logger.warning("✗ Trh nenalezen pro %s dne %s", city_polymarket_name, target_date)
        return None

    def get_market_price(self, slug: str) -> Optional[WeatherMarket]:
        """
        Získá aktuální cenu trhu podle slugu.
        Používá se pro monitoring otevřených pozic.
        """
        return self._fetch_by_slug(slug)

    def get_market_by_id(self, market_id: str) -> Optional[WeatherMarket]:
        """
        Získá trh podle ID.
        """
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{GAMMA_BASE}/markets/{market_id}")
                resp.raise_for_status()
                return self._parse_market(resp.json())
        except Exception as exc:
            logger.error("Chyba při získávání trhu %s: %s", market_id, exc)
            return None

    # ------------------------------------------------------------------
    # Vyhledávání
    # ------------------------------------------------------------------

    def _fetch_by_slug(self, slug: str) -> Optional[WeatherMarket]:
        """GET /markets/slug/{slug}"""
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{GAMMA_BASE}/markets/slug/{slug}")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    return self._parse_market(data[0]) if data else None
                return self._parse_market(data)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.error("HTTP chyba pro slug %s: %s", slug, exc)
            return None
        except Exception as exc:
            logger.error("Chyba pro slug %s: %s", slug, exc)
            return None

    def _search_weather_market(
        self, city_name: str, target_date: date
    ) -> Optional[WeatherMarket]:
        """
        Hledá weather market přes Gamma API fulltextové vyhledávání.
        Zkouší více variant query.
        """
        queries = [
            f"temperature {city_name} {target_date.strftime('%B %-d')}",
            f"high temperature {city_name}",
            f"temperature {city_name} {target_date.year}",
        ]

        for query in queries:
            results = self._search_markets(query)
            best = self._pick_best_match(results, city_name, target_date)
            if best:
                return best

        return None

    def _search_markets(self, query: str, limit: int = 20) -> list[WeatherMarket]:
        """GET /markets?q=...&active=true&closed=false"""
        try:
            with httpx.Client(timeout=self.timeout) as client:
                params = {
                    "q": query,
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                }
                resp = client.get(f"{GAMMA_BASE}/markets", params=params)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list):
                    return []
                return [m for m in [self._parse_market(d) for d in data] if m is not None]
        except Exception as exc:
            logger.error("Chyba search '%s': %s", query, exc)
            return []

    def _pick_best_match(
        self, markets: list[WeatherMarket], city_name: str, target_date: date
    ) -> Optional[WeatherMarket]:
        """
        Z výsledků vyhledávání vybere nejrelevantější trh.
        Bodovací logika: datum > město > klíčová slova.
        """
        date_strs = [
            target_date.strftime("%B %-d").lower(),
            target_date.strftime("%b %-d").lower(),
            target_date.strftime("%Y-%m-%d").lower(),
            target_date.strftime("%-m/%-d").lower(),
        ]
        city_lower = city_name.lower().replace("-", " ")

        best_score = -1
        best_market: Optional[WeatherMarket] = None

        for m in markets:
            if m.closed or not m.active:
                continue
            q_lower = m.question.lower()
            slug_lower = m.slug.lower()
            combined = f"{q_lower} {slug_lower}"

            score = 0
            # Boduje za datum
            for ds in date_strs:
                if ds in combined:
                    score += 10
                    break
            # Boduje za město
            if city_lower in combined or city_lower.replace(" ", "-") in combined:
                score += 5
            # Boduje za weather klíčová slova
            for kw in ["temperature", "high", "highest", "weather", "temp"]:
                if kw in combined:
                    score += 1

            if score > best_score:
                best_score = score
                best_market = m

        # Minimální skóre pro přijetí
        return best_market if best_score >= 5 else None

    # ------------------------------------------------------------------
    # Slug builder
    # ------------------------------------------------------------------

    def _build_slug(
        self,
        city_name: str,
        target_date: date,
        predicted_temp: float,
        unit: str,
    ) -> str:
        """
        Sestavuje slug pro Polymarket weather kontrakty.

        Polymarket weather market slug formáty:
          - highest-temperature-in-new-york-on-march-25-2026
          - will-high-temperature-in-chicago-exceed-75f-on-march-25
          - high-temp-new-york-above-65-march-25-2026

        Používáme nejčastější formát:
          highest-temperature-in-{city}-on-{month}-{day}-{year}
        """
        month = target_date.strftime("%B").lower()
        day = target_date.day
        year = target_date.year
        city_slug = city_name.lower().replace(" ", "-")
        temp_int = int(round(predicted_temp))

        # Primární slug formát (bez konkrétní teploty)
        return f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"

    # ------------------------------------------------------------------
    # Parsování odpovědi API
    # ------------------------------------------------------------------

    def _parse_market(self, data: dict) -> Optional[WeatherMarket]:
        """Parsuje raw API odpověď na WeatherMarket objekt."""
        if not data or not isinstance(data, dict):
            return None

        try:
            # Parsování outcomes a jejich cen
            outcomes_raw = data.get("outcomes", "[]")
            prices_raw = data.get("outcomePrices", "[]")

            if isinstance(outcomes_raw, str):
                import json
                try:
                    outcomes_raw = json.loads(outcomes_raw)
                except json.JSONDecodeError:
                    outcomes_raw = []

            if isinstance(prices_raw, str):
                import json
                try:
                    prices_raw = json.loads(prices_raw)
                except json.JSONDecodeError:
                    prices_raw = []

            outcomes: list[MarketOutcome] = []
            tokens = data.get("tokens", []) or []
            for i, name in enumerate(outcomes_raw):
                price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
                token_id = tokens[i].get("token_id", "") if i < len(tokens) else ""
                outcomes.append(MarketOutcome(name=str(name), token_id=token_id, price=price))

            # Ceny s fallbackem
            last_trade = float(data.get("lastTradePrice", 0) or 0)
            best_ask = float(data.get("bestAsk", 0) or 0)
            best_bid = float(data.get("bestBid", 0) or 0)

            # Pokud lastTradePrice chybí, zkusíme z outcomePrices (YES = index 0)
            if last_trade == 0 and prices_raw:
                last_trade = float(prices_raw[0]) if prices_raw else 0

            return WeatherMarket(
                market_id=str(data.get("id", "")),
                slug=str(data.get("slug", "")),
                question=str(data.get("question", "")),
                end_date=str(data.get("endDate", "")),
                last_trade_price=last_trade,
                best_ask=best_ask,
                best_bid=best_bid,
                active=bool(data.get("active", True)),
                closed=bool(data.get("closed", False)),
                outcomes=outcomes,
            )
        except Exception as exc:
            logger.error("Chyba parsování trhu: %s | data: %s", exc, str(data)[:200])
            return None


# ---------------------------------------------------------------------------
# Testovací spuštění
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from datetime import timedelta

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    gamma = PolymarketGamma()
    tomorrow = date.today() + timedelta(days=1)

    print("\n=== Test Polymarket Gamma API ===\n")

    # Test hledání weather marketů
    test_cities = [("new-york", 65.0, "F"), ("london", 12.0, "C")]
    for city, temp, unit in test_cities:
        print(f"Hledám trh pro {city} ({temp}°{unit}) dne {tomorrow}...")
        market = gamma.find_weather_market(city, tomorrow, temp, unit)
        if market:
            print(f"  Nalezen: {market.slug}")
            print(f"  Otázka: {market.question}")
            print(f"  Cena YES: {market.yes_price_pct:.1f}%")
            print(f"  lastTradePrice: {market.last_trade_price:.3f}")
            print(f"  bestAsk: {market.best_ask:.3f}")
        else:
            print(f"  Trh nenalezen pro {city}")
        print()
