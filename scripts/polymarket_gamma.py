"""
polymarket_gamma.py — Polymarket Gamma API klient (opravená verze)
=================================================================
Dokumentace: https://docs.polymarket.com/developers/gamma-markets-api/fetch-markets-guide

KRITICKÁ OPRAVA: Gamma API NEMÁ parametr ?q= pro textové vyhledávání.
Správné strategie (dle oficiální dokumentace):
  1. /events/slug/{slug}               — přesný event slug lookup
  2. /markets/slug/{slug}              — přesný market slug lookup  
  3. /events?tag_id=XXX&active=true    — filtrování dle kategorie (tag)
  4. /events?active=true&closed=false  — kompletní scan s filtrací

Hierarchie Polymarket:
  Event  (např. "Temperature in New York March 25")
    └── Market[]  (konkrétní prahové hodnoty, YES/NO kontrakty)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

WEATHER_TAG_CANDIDATES = ["weather", "temperature", "climate"]
_weather_tag_id_cache: Optional[str] = None


@dataclass
class MarketOutcome:
    name: str
    token_id: str
    price: float


@dataclass
class WeatherMarket:
    market_id: str
    event_id: str
    event_slug: str
    market_slug: str
    question: str
    end_date: str
    last_trade_price: float
    best_ask: float
    best_bid: float
    active: bool
    closed: bool
    outcomes: list[MarketOutcome] = field(default_factory=list)

    @property
    def yes_price(self) -> float:
        for o in self.outcomes:
            if o.name.lower() == "yes":
                return o.price
        return self.last_trade_price

    @property
    def yes_price_pct(self) -> float:
        return round(self.yes_price * 100, 2)


class PolymarketGamma:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Veřejné metody
    # ------------------------------------------------------------------

    def find_weather_market(self, city_polymarket_name: str, target_date: date,
                            predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
        # Strategie 1: event slug
        m = self._strategy_event_slug(city_polymarket_name, target_date)
        if m: return m
        # Strategie 2: market slug
        m = self._strategy_market_slug(city_polymarket_name, target_date, predicted_temp, unit)
        if m: return m
        # Strategie 3: tag filtrování
        m = self._strategy_tag_filter(city_polymarket_name, target_date, predicted_temp, unit)
        if m: return m
        # Strategie 4: scan aktivních events
        m = self._strategy_events_scan(city_polymarket_name, target_date, predicted_temp, unit)
        if m: return m
        logger.warning("✗ Trh nenalezen: %s %s", city_polymarket_name, target_date)
        return None

    def get_market_price(self, market_slug: str) -> Optional[WeatherMarket]:
        return self._fetch_market_by_slug(market_slug)

    def get_weather_tag_id(self) -> Optional[str]:
        global _weather_tag_id_cache
        if _weather_tag_id_cache:
            return _weather_tag_id_cache
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{GAMMA_BASE}/tags", params={"limit": 200})
                resp.raise_for_status()
                tags = resp.json()
            if not isinstance(tags, list):
                return None
            for tag in tags:
                label = str(tag.get("label", "")).lower()
                slug  = str(tag.get("slug",  "")).lower()
                for c in WEATHER_TAG_CANDIDATES:
                    if c in label or c in slug:
                        tid = str(tag.get("id", ""))
                        logger.info("Weather tag: id=%s label=%s", tid, tag.get("label"))
                        _weather_tag_id_cache = tid
                        return tid
            logger.warning("Weather tag nenalezen v /tags")
            return None
        except Exception as exc:
            logger.error("/tags chyba: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Strategie 1: event slug lookup
    # ------------------------------------------------------------------

    def _strategy_event_slug(self, city: str, target_date: date) -> Optional[WeatherMarket]:
        for slug in self._generate_event_slugs(city, target_date):
            event = self._fetch_event_by_slug(slug)
            if event:
                m = self._extract_best_market_from_event(event)
                if m:
                    logger.info("✓ [event_slug] %s → %s", slug, m.market_slug)
                    return m
        return None

    def _generate_event_slugs(self, city: str, target_date: date) -> list[str]:
        c     = city.lower().replace(" ", "-")
        month = target_date.strftime("%B").lower()
        mabbr = target_date.strftime("%b").lower()
        d     = target_date.day
        y     = target_date.year
        return [
            f"highest-temperature-in-{c}-{month}-{d}-{y}",
            f"highest-temperature-in-{c}-on-{month}-{d}-{y}",
            f"high-temperature-in-{c}-{month}-{d}-{y}",
            f"high-temp-in-{c}-{month}-{d}-{y}",
            f"temperature-in-{c}-{month}-{d}-{y}",
            f"temperature-{c}-{month}-{d}-{y}",
            f"daily-high-temperature-{c}-{month}-{d}",
            f"daily-high-temperature-in-{c}-{month}-{d}",
            f"highest-temperature-in-{c}-{month}-{d}",
            f"high-temperature-{c}-{month}-{d}",
            f"highest-temperature-in-{c}-{mabbr}-{d}-{y}",
        ]

    # ------------------------------------------------------------------
    # Strategie 2: market slug lookup
    # ------------------------------------------------------------------

    def _strategy_market_slug(self, city: str, target_date: date,
                               predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
        c     = city.lower().replace(" ", "-")
        t     = int(round(predicted_temp))
        month = target_date.strftime("%B").lower()
        d     = target_date.day
        y     = target_date.year
        u     = unit.lower()
        slugs = [
            f"will-the-high-temperature-in-{c}-exceed-{t}{u}-on-{month}-{d}",
            f"will-the-high-temperature-in-{c}-exceed-{t}{u}-on-{month}-{d}-{y}",
            f"will-high-temperature-in-{c}-exceed-{t}{u}-{month}-{d}-{y}",
            f"highest-temperature-in-{c}-on-{month}-{d}-{y}",
            f"will-the-high-temperature-in-{c}-be-above-{t}{u}-on-{month}-{d}",
        ]
        for slug in slugs:
            m = self._fetch_market_by_slug(slug)
            if m and m.active and not m.closed:
                logger.info("✓ [market_slug] %s", slug)
                return m
        return None

    # ------------------------------------------------------------------
    # Strategie 3: tag filtrování
    # ------------------------------------------------------------------

    def _strategy_tag_filter(self, city: str, target_date: date,
                          predicted_temp: float = 0.0, unit: str = "F") -> Optional[WeatherMarket]:
        tag_id = self.get_weather_tag_id()
        if not tag_id:
            return None
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{GAMMA_BASE}/events", params={
                    "tag_id": tag_id, "active": "true", "closed": "false",
                    "limit": 100, "related_tags": "true",
                })
                resp.raise_for_status()
                events = resp.json()
            if not isinstance(events, list):
                return None
            return self._find_in_events(events, city, target_date, predicted_temp, unit)
        except Exception as exc:
            logger.error("tag filtrování chyba: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Strategie 4: scan events
    # ------------------------------------------------------------------

    def _strategy_events_scan(self, city: str, target_date: date,
                               predicted_temp: float = 0.0, unit: str = "F",
                               max_pages: int = 5) -> Optional[WeatherMarket]:
        limit = 50
        for page in range(max_pages):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(f"{GAMMA_BASE}/events", params={
                        "active": "true", "closed": "false",
                        "limit": limit, "offset": page * limit,
                        "order": "id", "ascending": "false",
                    })
                    resp.raise_for_status()
                    events = resp.json()
                if not isinstance(events, list) or not events:
                    break
                m = self._find_in_events(events, city, target_date, predicted_temp, unit)
                if m:
                    return m
                if len(events) < limit:
                    break
            except Exception as exc:
                logger.error("events scan str. %d: %s", page, exc)
                break
        return None

    # ------------------------------------------------------------------
    # HTTP helpery
    # ------------------------------------------------------------------

    def _fetch_event_by_slug(self, slug: str) -> Optional[dict]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{GAMMA_BASE}/events/slug/{slug}")
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        return data[0] if data else None
                    return data or None
                if resp.status_code == 404:
                    resp2 = client.get(f"{GAMMA_BASE}/events", params={"slug": slug})
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        if isinstance(data2, list):
                            return data2[0] if data2 else None
                        return data2 or None
            return None
        except Exception as exc:
            logger.debug("_fetch_event_by_slug(%s): %s", slug, exc)
            return None

    def _fetch_market_by_slug(self, slug: str) -> Optional[WeatherMarket]:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(f"{GAMMA_BASE}/markets/slug/{slug}")
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data[0] if isinstance(data, list) else data
                    return self._parse_market(raw)
                if resp.status_code == 404:
                    resp2 = client.get(f"{GAMMA_BASE}/markets", params={"slug": slug})
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        raw2 = data2[0] if isinstance(data2, list) else data2
                        return self._parse_market(raw2)
            return None
        except Exception as exc:
            logger.debug("_fetch_market_by_slug(%s): %s", slug, exc)
            return None

    # ------------------------------------------------------------------
    # Parsování
    # ------------------------------------------------------------------

    def _find_in_events(self, events: list[dict], city: str,
                        target_date: date, predicted_temp: float = 0.0,
                        unit: str = "F") -> Optional[WeatherMarket]:
        city_lower = city.lower().replace("-", " ")
        date_strs  = [
            target_date.strftime("%B %-d").lower(),
            target_date.strftime("%b %-d").lower(),
            target_date.strftime("%Y-%m-%d"),
        ]
        month_str = target_date.strftime("%B").lower()

        best_score = -1
        best_market: Optional[WeatherMarket] = None

        for event in events:
            if not isinstance(event, dict): continue
            if event.get("closed") or not event.get("active", True): continue

            combined = (str(event.get("slug","")) + " " + str(event.get("title",""))).lower()
            has_city    = (city_lower in combined or city_lower.replace(" ","-") in combined)
            has_weather = any(kw in combined for kw in ["temperature","temp","weather","high","heat"])
            if not (has_city and has_weather): continue

            score = 10 if has_city else 0
            for ds in date_strs:
                if ds in combined: score += 8; break
            if month_str in combined: score += 3
            for kw in ["temperature","highest","daily high"]:
                if kw in combined: score += 2

            if score > best_score:
                eslug = str(event.get("slug",""))
                eid   = str(event.get("id",""))
                markets_raw = event.get("markets") or []
                m = self._closest_to_prediction(
                    markets_raw, eslug, eid, predicted_temp, unit)
                if not m and not markets_raw:
                    m = self._event_as_market(event)
                if m and m.active and not m.closed:
                    best_score  = score
                    best_market = m

        return best_market if best_score >= 10 else None

    def _extract_best_market_from_event(self, event: dict) -> Optional[WeatherMarket]:
        if not event: return None
        eslug = str(event.get("slug",""))
        eid   = str(event.get("id",""))
        markets_raw = event.get("markets") or []
        # predicted_temp/unit musí být předány z kontextu — jako fallback bez nich
        # použijeme _closest_to_prediction pokud jsou k dispozici, jinak první aktivní
        m = self._closest_to_fair_fallback(markets_raw, eslug, eid)
        if m:
            return m
        return self._event_as_market(event)

    def _event_as_market(self, event: dict) -> Optional[WeatherMarket]:
        try:
            prices_raw   = event.get("outcomePrices","[]") or "[]"
            outcomes_raw = event.get("outcomes","[]")       or "[]"
            if isinstance(prices_raw,   str): prices_raw   = json.loads(prices_raw)
            if isinstance(outcomes_raw, str): outcomes_raw = json.loads(outcomes_raw)
            last_trade = float(event.get("lastTradePrice",0) or 0)
            if last_trade == 0 and prices_raw:
                last_trade = float(prices_raw[0])
            return WeatherMarket(
                market_id=str(event.get("id","")),
                event_id=str(event.get("id","")),
                event_slug=str(event.get("slug","")),
                market_slug=str(event.get("slug","")),
                question=str(event.get("title", event.get("question",""))),
                end_date=str(event.get("endDate","")),
                last_trade_price=last_trade,
                best_ask=float(event.get("bestAsk",0) or 0),
                best_bid=float(event.get("bestBid",0) or 0),
                active=bool(event.get("active",True)),
                closed=bool(event.get("closed",False)),
            )
        except Exception as exc:
            logger.debug("_event_as_market: %s", exc)
            return None


    def _closest_to_fair_fallback(self, markets_raw: list[dict], event_slug: str,
                                   event_id: str) -> Optional[WeatherMarket]:
        """Fallback bez znalosti predicted_temp: vezme první aktivní market."""
        for m_raw in (markets_raw or []):
            m = self._parse_market(m_raw, event_slug, event_id)
            if m and m.active and not m.closed:
                return m
        return None

    def _closest_to_prediction(self, markets_raw: list[dict], event_slug: str,
                                event_id: str, predicted_temp: float,
                                unit: str) -> Optional[WeatherMarket]:
        """
        Vybere market jehož práh nejlépe odpovídá předpovědi.

        Logika výběru:
          1. °C předpověď se VŽDY zaokrouhlí na celé číslo (15.7 → 16).
          2. Hledá se exact match zaokrouhleného čísla.
          3. Pokud exact match neexistuje, vybere nejbližší "X or below"
             nebo "X or above" kontrakt (opět dle zaokrouhleného čísla).

        Směr kontraktu:
          - above/exceed/over  → YES = teplota NAD prahem
          - below/under/orbelow → YES = teplota POD prahem

        Odmítá kontrakt kde forecast je na špatné straně prahu
        (např. forecast 16°C a market "15°C or below" → YES≈7% → přeskočit).
        """
        import re

        # Pro °C vždy zaokrouhli na celé číslo
        # Standardní zaokrouhlení (ne banker's): 15.5→16, 16.5→17
        target = int(predicted_temp + 0.5) if unit.upper() == "C" else predicted_temp
        logger.info("  Hledám práh: %.1f°%s → cíl=%g°%s",
                    predicted_temp, unit, target, unit)

        ABOVE_KW = ["exceed", "above", "over", "higher", "more than", "greater"]
        BELOW_KW = ["below", "under", "orbelow", "or-below", "or below",
                    "less than", "lower", "at most", "atmost"]

        def detect_direction(question: str, slug: str) -> str:
            combined = (question + " " + slug).lower()
            for kw in BELOW_KW:
                if kw in combined:
                    return "below"
            for kw in ABOVE_KW:
                if kw in combined:
                    return "above"
            return "unknown"

        def extract_threshold(question: str, slug: str, unit: str) -> Optional[float]:
            u = unit.upper()
            patterns = [
                rf"(\d+(?:\.\d+)?)[°\s]*{u}",       # "65°F", "12°C"
                rf"(\d+(?:\.\d+)?){u.lower()}",        # "65f", "12c"
                rf"exceed[\s-](\d+(?:\.\d+)?)",
                rf"above[\s-](\d+(?:\.\d+)?)",
                rf"below[\s-](\d+(?:\.\d+)?)",
                rf"-(\d+(?:\.\d+)?)[cf](?:-|$|or)",   # "-15c-", "-15corbelow"
                rf"-(\d+(?:\.\d+)?)-",                 # "-65-" fallback
            ]
            for text in [question, slug]:
                for pat in patterns:
                    m = re.search(pat, text, re.IGNORECASE)
                    if m:
                        return float(m.group(1))
            return None

        # Parsuj všechny aktivní markety
        parsed: list[tuple[float, str, WeatherMarket]] = []  # (threshold, direction, market)
        for m_raw in (markets_raw or []):
            m = self._parse_market(m_raw, event_slug, event_id)
            if not m or not m.active or m.closed:
                continue
            direction = detect_direction(m.question, m.market_slug)
            threshold = extract_threshold(m.question, m.market_slug, unit)
            if threshold is None:
                logger.debug("  Přeskočen (práh nenalezen): %s", m.market_slug)
                continue
            parsed.append((threshold, direction, m))
            logger.debug("  Parsován: %s | práh=%g | směr=%s",
                         m.market_slug, threshold, direction)

        if not parsed:
            return None

        # --- Krok 1: exact match zaokrouhleného čísla ---
        exact = [(t, d, m) for t, d, m in parsed if t == target]
        if exact:
            # Preferuj "above/exceed" u exact matche (přirozenější kontrakt)
            above_exact = [(t,d,m) for t,d,m in exact if d in ("above","unknown")]
            chosen = above_exact[0] if above_exact else exact[0]
            logger.info("  ✓ Exact match: %s | práh=%g°%s | směr=%s",
                        chosen[2].market_slug, chosen[0], unit, chosen[1])
            return chosen[2]

        # --- Krok 2: nejbližší smysluplný kontrakt --
