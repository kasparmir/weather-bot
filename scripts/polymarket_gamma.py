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
        m = self._strategy_tag_filter(city_polymarket_name, target_date)
        if m: return m
        # Strategie 4: scan aktivních events
        m = self._strategy_events_scan(city_polymarket_name, target_date)
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

    def _strategy_tag_filter(self, city: str, target_date: date) -> Optional[WeatherMarket]:
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
            return self._find_in_events(events, city, target_date)
        except Exception as exc:
            logger.error("tag filtrování chyba: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Strategie 4: scan events
    # ------------------------------------------------------------------

    def _strategy_events_scan(self, city: str, target_date: date,
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
                m = self._find_in_events(events, city, target_date)
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
                    return (data[0] if isinstance(data, list) else data) or None
                if resp.status_code == 404:
                    resp2 = client.get(f"{GAMMA_BASE}/events", params={"slug": slug})
                    if resp2.status_code == 200:
                        data2 = resp2.json()
                        return (data2[0] if isinstance(data2, list) else data2) or None
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
                        target_date: date) -> Optional[WeatherMarket]:
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
                for m_raw in (event.get("markets") or []):
                    m = self._parse_market(m_raw, eslug, eid)
                    if m and m.active and not m.closed:
                        best_score  = score
                        best_market = m
                        break
                # Pokud event nemá embedded markets, zkus event samotný
                if best_score != score:
                    m = self._event_as_market(event)
                    if m and m.active and not m.closed:
                        best_score  = score
                        best_market = m

        return best_market if best_score >= 10 else None

    def _extract_best_market_from_event(self, event: dict) -> Optional[WeatherMarket]:
        if not event: return None
        eslug = str(event.get("slug",""))
        eid   = str(event.get("id",""))
        for m_raw in (event.get("markets") or []):
            m = self._parse_market(m_raw, eslug, eid)
            if m and m.active and not m.closed:
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

    def _parse_market(self, data: dict, event_slug: str = "",
                      event_id: str = "") -> Optional[WeatherMarket]:
        if not data or not isinstance(data, dict): return None
        try:
            outcomes_raw = data.get("outcomes","[]")       or "[]"
            prices_raw   = data.get("outcomePrices","[]") or "[]"
            if isinstance(outcomes_raw, str): outcomes_raw = json.loads(outcomes_raw)
            if isinstance(prices_raw,   str): prices_raw   = json.loads(prices_raw)

            tokens   = data.get("tokens",[])       or []
            clob_ids = data.get("clobTokenIds",[]) or []
            outcomes: list[MarketOutcome] = []
            for i, name in enumerate(outcomes_raw):
                price    = float(prices_raw[i]) if i < len(prices_raw) else 0.5
                token_id = ""
                if   i < len(tokens)   and isinstance(tokens[i], dict):
                    token_id = str(tokens[i].get("token_id",""))
                elif i < len(clob_ids):
                    token_id = str(clob_ids[i])
                outcomes.append(MarketOutcome(name=str(name), token_id=token_id, price=price))

            last_trade = float(data.get("lastTradePrice",0) or 0)
            best_ask   = float(data.get("bestAsk",0) or 0)
            best_bid   = float(data.get("bestBid",0) or 0)
            if last_trade == 0 and prices_raw:
                last_trade = float(prices_raw[0])

            eid   = event_id   or str(data.get("eventId",""))
            eslug = event_slug or str(data.get("eventSlug",""))

            return WeatherMarket(
                market_id=str(data.get("id", data.get("conditionId",""))),
                event_id=eid,
                event_slug=eslug,
                market_slug=str(data.get("slug", data.get("marketSlug", eslug))),
                question=str(data.get("question","")),
                end_date=str(data.get("endDate", data.get("endDateIso",""))),
                last_trade_price=last_trade,
                best_ask=best_ask,
                best_bid=best_bid,
                active=bool(data.get("active",True)),
                closed=bool(data.get("closed",False)),
                outcomes=outcomes,
            )
        except Exception as exc:
            logger.error("_parse_market: %s | %.200s", exc, str(data))
            return None


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    from datetime import timedelta

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    gamma = PolymarketGamma()
    tomorrow = date.today() + timedelta(days=1)

    print(f"\n=== Test Polymarket Gamma API ===")
    print(f"Target date: {tomorrow}\n")

    print("1. Weather tag ID...")
    tag_id = gamma.get_weather_tag_id()
    print(f"   → {tag_id or 'nenalezen'}\n")

    for city, temp, unit in [("new-york",65.0,"F"),("london",12.0,"C"),("chicago",58.0,"F")]:
        print(f"Hledám: {city} | {temp}°{unit}")
        m = gamma.find_weather_market(city, tomorrow, temp, unit)
        if m:
            print(f"  ✓ slug:     {m.market_slug}")
            print(f"  ✓ question: {m.question[:80]}")
            print(f"  ✓ YES:      {m.yes_price_pct:.1f}%")
        else:
            print(f"  ✗ Nenalezen (weather market pro {tomorrow} nemusí ještě existovat)")
        print()
