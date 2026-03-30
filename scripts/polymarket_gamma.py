"""
polymarket_gamma.py — Polymarket Gamma API klient
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
WEATHER_TAG_CANDIDATES = ["weather", "temperature", "climate"]
_weather_tag_id_cache: Optional[str] = None

ABOVE_KW = ["exceed", "above", "over", "higher", "more than", "greater"]
BELOW_KW = ["below", "under", "orbelow", "or-below", "or below", "less than", "lower", "at most"]


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
        strategies = [
            lambda: self._strategy_event_slug(city_polymarket_name, target_date, predicted_temp, unit),
            lambda: self._strategy_market_slug(city_polymarket_name, target_date, predicted_temp, unit),
            lambda: self._strategy_tag_filter(city_polymarket_name, target_date, predicted_temp, unit),
            lambda: self._strategy_events_scan(city_polymarket_name, target_date, predicted_temp, unit),
        ]
        for strategy in strategies:
            m = strategy()
            if not m:
                continue
            if not self._validate_market_date(m, target_date):
                logger.warning(
                    "✗ Market odmítnut — datum nesedí: end_date='%s' target=%s (%s)",
                    m.end_date, target_date, m.market_slug,
                )
                continue
            return m
        logger.warning("✗ Trh nenalezen: %s %s", city_polymarket_name, target_date)
        return None

    def _validate_market_date(self, market: "WeatherMarket", target_date: date) -> bool:
        """
        Ověří, že market settlement date odpovídá target_date (±1 den tolerance
        kvůli timezone offset UTC vs lokální čas).

        Polymarket end_date formáty: "2026-03-27T23:59:00Z", "2026-03-27T00:00:00Z"
        Pokud end_date chybí nebo nejde parsovat → přijmeme (benefit of doubt).
        """
        end_raw = (market.end_date or "").strip()
        if not end_raw:
            return True

        try:
            market_date = date.fromisoformat(end_raw[:10])
        except (ValueError, TypeError):
            logger.debug("_validate_market_date: nelze parsovat end_date '%s'", end_raw)
            return True

        diff = abs((market_date - target_date).days)
        if diff > 1:
            logger.warning(
                "  Date mismatch: market_date=%s, target=%s, Δ=%d dní — odmítám",
                market_date, target_date, diff,
            )
            return False

        logger.debug("  Date OK: market_date=%s, target=%s", market_date, target_date)
        return True

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
                slug = str(tag.get("slug", "")).lower()
                for c in WEATHER_TAG_CANDIDATES:
                    if c in label or c in slug:
                        tid = str(tag.get("id", ""))
                        logger.info("Weather tag: id=%s label=%s", tid, tag.get("label"))
                        _weather_tag_id_cache = tid
                        return tid
            logger.warning("Weather tag nenalezen")
            return None
        except Exception as exc:
            logger.error("/tags chyba: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Strategie 1: event slug
    # ------------------------------------------------------------------

    def _strategy_event_slug(self, city: str, target_date: date,
                              predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
        for slug in self._generate_event_slugs(city, target_date):
            event = self._fetch_event_by_slug(slug)
            if event:
                m = self._best_market_from_event(event, predicted_temp, unit)
                if m:
                    logger.info("✓ [event_slug] %s → %s", slug, m.market_slug)
                    return m
        return None

    def _generate_event_slugs(self, city: str, target_date: date) -> list[str]:
        c = city.lower().replace(" ", "-")
        month = target_date.strftime("%B").lower()
        mabbr = target_date.strftime("%b").lower()
        d = target_date.day
        y = target_date.year
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
    # Strategie 2: market slug
    # ------------------------------------------------------------------

    def _strategy_market_slug(self, city: str, target_date: date,
                               predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
        c = city.lower().replace(" ", "-")
        # Standardní zaokrouhlení pro obě jednotky (bez banker's rounding)
        t = int(predicted_temp + 0.5) if unit.upper() == "C" else int(predicted_temp + 0.5)
        month = target_date.strftime("%B").lower()
        d = target_date.day
        y = target_date.year
        u = unit.lower()
        for slug in [
            f"will-the-high-temperature-in-{c}-exceed-{t}{u}-on-{month}-{d}",
            f"will-the-high-temperature-in-{c}-exceed-{t}{u}-on-{month}-{d}-{y}",
            f"will-high-temperature-in-{c}-exceed-{t}{u}-{month}-{d}-{y}",
            f"highest-temperature-in-{c}-on-{month}-{d}-{y}",
            f"will-the-high-temperature-in-{c}-be-above-{t}{u}-on-{month}-{d}",
        ]:
            m = self._fetch_market_by_slug(slug)
            if m and m.active and not m.closed:
                logger.info("✓ [market_slug] %s", slug)
                return m
        return None

    # ------------------------------------------------------------------
    # Strategie 3: tag filtrování
    # ------------------------------------------------------------------

    def _strategy_tag_filter(self, city: str, target_date: date,
                              predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
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
    # Strategie 4: scan
    # ------------------------------------------------------------------

    def _strategy_events_scan(self, city: str, target_date: date,
                               predicted_temp: float, unit: str,
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
    # Pomocné metody pro výběr marketu
    # ------------------------------------------------------------------

    def _find_in_events(self, events: list[dict], city: str, target_date: date,
                        predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
        city_lower = city.lower().replace("-", " ")
        date_strs = [
            target_date.strftime("%B %-d").lower(),
            target_date.strftime("%b %-d").lower(),
            target_date.strftime("%Y-%m-%d"),
        ]
        month_str = target_date.strftime("%B").lower()

        best_score = -1
        best_market: Optional[WeatherMarket] = None

        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("closed") or not event.get("active", True):
                continue
            combined = (str(event.get("slug", "")) + " " + str(event.get("title", ""))).lower()
            has_city = city_lower in combined or city_lower.replace(" ", "-") in combined
            has_weather = any(kw in combined for kw in ["temperature", "temp", "weather", "high", "heat"])
            if not (has_city and has_weather):
                continue

            score = 10 if has_city else 0
            date_found = False
            for ds in date_strs:
                if ds in combined:
                    score += 8
                    date_found = True
                    break
            if month_str in combined:
                score += 3
            for kw in ["temperature", "highest", "daily high"]:
                if kw in combined:
                    score += 2

            # Penalizace pokud datum není v event slug/title
            # (bude ověřeno přes end_date validaci, ale preferujeme explicitní datum)
            if not date_found:
                score -= 5

            if score > best_score:
                eslug = str(event.get("slug", ""))
                eid = str(event.get("id", ""))
                m = self._best_market_from_event(event, predicted_temp, unit)
                if not m:
                    m = self._event_as_market(event)
                if m and m.active and not m.closed:
                    best_score = score
                    best_market = m

        return best_market if best_score >= 10 else None

    def _best_market_from_event(self, event: dict,
                                 predicted_temp: float, unit: str) -> Optional[WeatherMarket]:
        """Vybere z event.markets[] ten jehož práh nejlépe odpovídá předpovědi."""
        if not event:
            return None
        eslug = str(event.get("slug", ""))
        eid = str(event.get("id", ""))
        markets_raw = event.get("markets") or []
        if not markets_raw:
            return None
        return self._closest_to_prediction(markets_raw, eslug, eid, predicted_temp, unit)

    def _event_as_market(self, event: dict) -> Optional[WeatherMarket]:
        try:
            prices_raw = event.get("outcomePrices", "[]") or "[]"
            outcomes_raw = event.get("outcomes", "[]") or "[]"
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)
            if isinstance(outcomes_raw, str):
                outcomes_raw = json.loads(outcomes_raw)
            last_trade = float(event.get("lastTradePrice", 0) or 0)
            if last_trade == 0 and prices_raw:
                last_trade = float(prices_raw[0])
            return WeatherMarket(
                market_id=str(event.get("id", "")),
                event_id=str(event.get("id", "")),
                event_slug=str(event.get("slug", "")),
                market_slug=str(event.get("slug", "")),
                question=str(event.get("title", event.get("question", ""))),
                end_date=str(event.get("endDate", "")),
                last_trade_price=last_trade,
                best_ask=float(event.get("bestAsk", 0) or 0),
                best_bid=float(event.get("bestBid", 0) or 0),
                active=bool(event.get("active", True)),
                closed=bool(event.get("closed", False)),
            )
        except Exception as exc:
            logger.debug("_event_as_market: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Výběr marketu nejbližšího k předpovědi
    # ------------------------------------------------------------------

    def _closest_to_prediction(self, markets_raw: list[dict], event_slug: str,
                                event_id: str, predicted_temp: float,
                                unit: str) -> Optional[WeatherMarket]:
        """
        Vybere market jehož teplotní práh nejlépe odpovídá předpovědi.

        Logika:
          1. °C vždy zaokrouhlíme na celé číslo (15.7 → 16)
          2. Hledáme exact match zaokrouhleného čísla (priorita)
          3. Pokud nenajdeme, vezmeme nejbližší X or below / X or above
             kde forecast je na správné straně prahu
        """
        # Pro °C zaokrouhlujeme standardně (ne banker's rounding)
        target = int(predicted_temp + 0.5) if unit.upper() == "C" else predicted_temp
        logger.info("  Hledám práh: %.1f°%s → cíl=%g°%s", predicted_temp, unit, target, unit)

        parsed: list[tuple[float, str, WeatherMarket]] = []
        for m_raw in (markets_raw or []):
            m = self._parse_market(m_raw, event_slug, event_id)
            if not m or not m.active or m.closed:
                continue
            direction = self._detect_direction(m.question, m.market_slug)

            # Range market (např. "50-51f"): přeskoč pokud forecast leží mimo [lo, hi]
            rng = self._extract_range(m.market_slug, unit)
            if rng is not None:
                lo, hi = rng
                if lo <= target <= hi:
                    # Forecast přesně uvnitř range → perfektní match, Δ=0
                    parsed.append((target, "range", m))
                    logger.debug("  Parsován (range %g-%g, forecast IN): %s", lo, hi, m.market_slug)
                else:
                    logger.debug("  Přeskočen (range %g-%g, forecast %g OUT): %s",
                                 lo, hi, target, m.market_slug)
                continue  # range market zpracován, přejdi na další

            threshold = self._extract_threshold(m.question, m.market_slug, unit)
            if threshold is None:
                logger.debug("  Přeskočen (práh nenalezen): %s", m.market_slug)
                continue
            parsed.append((threshold, direction, m))
            logger.debug("  Parsován: %s | práh=%g | směr=%s", m.market_slug, threshold, direction)

        if not parsed:
            return None

        # Krok 1: range market (forecast uvnitř rozsahu) — absolutní priorita
        range_matches = [(t, d, m) for t, d, m in parsed if d == "range"]
        if range_matches:
            best = range_matches[0][2]
            logger.info("  ✓ Range match: %s", best.market_slug)
            return best

        # Krok 2: exact match zaokrouhleného čísla
        exact = [(t, d, m) for t, d, m in parsed if t == target]
        if exact:
            above_exact = [(t, d, m) for t, d, m in exact if d in ("above", "unknown")]
            chosen = above_exact[0] if above_exact else exact[0]
            logger.info("  ✓ Exact match: %s | práh=%g°%s | směr=%s",
                        chosen[2].market_slug, chosen[0], unit, chosen[1])
            return chosen[2]

        # Krok 2: nejbližší smysluplný
        candidates: list[tuple[float, WeatherMarket, str]] = []
        for threshold, direction, m in parsed:
            if direction == "below" and target > threshold:
                logger.debug("  Skip (below, forecast %g > práh %g): %s", target, threshold, m.market_slug)
                continue
            if direction == "above" and target < threshold:
                logger.debug("  Skip (above, forecast %g < práh %g): %s", target, threshold, m.market_slug)
                continue
            dist = abs(threshold - target)
            candidates.append((dist, m, direction))

        if not candidates:
            logger.info("  Žádný smysluplný kandidát")
            return None

        candidates.sort(key=lambda x: x[0])
        best_dist, best, best_dir = candidates[0]
        logger.info("  ✓ Nejbližší: %s | Δ=%g°%s | směr=%s", best.market_slug, best_dist, unit, best_dir)
        return best

    def _detect_direction(self, question: str, slug: str) -> str:
        combined = (question + " " + slug).lower()
        for kw in BELOW_KW:
            if kw in combined:
                return "below"
        for kw in ABOVE_KW:
            if kw in combined:
                return "above"
        return "unknown"

    def _extract_range(self, slug: str, unit: str) -> Optional[tuple[float, float]]:
        """
        Detekuje range market slug jako '50-51f' nebo '10-11c'.
        Vrací (lo, hi) nebo None.

        Omezení: max 3 cifry v každém čísle → nepřijme letopočet (2026).
        Teploty jsou reálně v rozsahu -50..150°F / -40..60°C.
        """
        ul = unit.lower()
        # \d{1,3} místo \d+ — zabrání zachycení roku jako čísla
        m = re.search(rf"-(\d{{1,3}}(?:\.\d+)?)-(\d{{1,3}}(?:\.\d+)?){ul}(?:-|$|or)",
                      slug, re.IGNORECASE)
        if m:
            lo, hi = float(m.group(1)), float(m.group(2))
            # Základní sanity check: lo < hi a hodnoty dávají smysl
            if lo < hi and lo >= -50 and hi <= 150:
                return lo, hi
        return None

    def _extract_threshold(self, question: str, slug: str, unit: str) -> Optional[float]:
        """
        Extrahuje teplotní práh.
        Pro range markety ('50-51f') vrátí střed rozsahu.
        """
        # Range market má přednost — musí být detekován dřív než single patterns
        # aby "-50-51f" nevyprodukovalo 50 z posledního pattern "-(\d+)-"
        rng = self._extract_range(slug, unit)
        if rng is not None:
            return (rng[0] + rng[1]) / 2  # střed rozsahu pro Δ výpočet

        u = unit.upper()
        patterns = [
            rf"(\d+(?:\.\d+)?)[°\s]*{u}",
            rf"(\d+(?:\.\d+)?){u.lower()}",
            rf"exceed[\s-](\d+(?:\.\d+)?)",
            rf"above[\s-](\d+(?:\.\d+)?)",
            rf"below[\s-](\d+(?:\.\d+)?)",
            rf"-(\d+(?:\.\d+)?)[cf](?:-|$|or)",
            rf"-(\d+(?:\.\d+)?)-",
        ]
        for text in [question, slug]:
            for pat in patterns:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    return float(m.group(1))
        return None

    # ------------------------------------------------------------------
    # Parsování raw market dict → WeatherMarket
    # ------------------------------------------------------------------

    def _parse_market(self, data: dict, event_slug: str = "",
                      event_id: str = "") -> Optional[WeatherMarket]:
        if not data or not isinstance(data, dict):
            return None
        try:
            outcomes_raw = data.get("outcomes", "[]") or "[]"
            prices_raw = data.get("outcomePrices", "[]") or "[]"
            if isinstance(outcomes_raw, str):
                outcomes_raw = json.loads(outcomes_raw)
            if isinstance(prices_raw, str):
                prices_raw = json.loads(prices_raw)

            tokens = data.get("tokens", []) or []
            clob_ids = data.get("clobTokenIds", []) or []
            outcomes: list[MarketOutcome] = []
            for i, name in enumerate(outcomes_raw):
                price = float(prices_raw[i]) if i < len(prices_raw) else 0.5
                token_id = ""
                if i < len(tokens) and isinstance(tokens[i], dict):
                    token_id = str(tokens[i].get("token_id", ""))
                elif i < len(clob_ids):
                    token_id = str(clob_ids[i])
                outcomes.append(MarketOutcome(name=str(name), token_id=token_id, price=price))

            last_trade = float(data.get("lastTradePrice", 0) or 0)
            best_ask = float(data.get("bestAsk", 0) or 0)
            best_bid = float(data.get("bestBid", 0) or 0)
            if last_trade == 0 and prices_raw:
                last_trade = float(prices_raw[0])

            eid = event_id or str(data.get("eventId", ""))
            eslug = event_slug or str(data.get("eventSlug", ""))

            return WeatherMarket(
                market_id=str(data.get("id", data.get("conditionId", ""))),
                event_id=eid,
                event_slug=eslug,
                market_slug=str(data.get("slug", data.get("marketSlug", eslug))),
                question=str(data.get("question", "")),
                end_date=str(data.get("endDate", data.get("endDateIso", ""))),
                last_trade_price=last_trade,
                best_ask=best_ask,
                best_bid=best_bid,
                active=bool(data.get("active", True)),
                closed=bool(data.get("closed", False)),
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

    print(f"\n=== Test Polymarket Gamma API === target: {tomorrow}\n")

    tag_id = gamma.get_weather_tag_id()
    print(f"Weather tag ID: {tag_id or 'nenalezen'}\n")

    for city, temp, unit in [("new-york", 65.0, "F"), ("london", 12.0, "C"), ("madrid", 16.0, "C")]:
        m = gamma.find_weather_market(city, tomorrow, temp, unit)
        if m:
            print(f"✓ {city}: {m.market_slug} | YES={m.yes_price_pct:.1f}%")
        else:
            print(f"✗ {city}: nenalezen")