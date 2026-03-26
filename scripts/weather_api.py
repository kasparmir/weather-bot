"""
weather_api.py — WeatherCollector
Sbírá reálná data předpovědí z NOAA (USA) a Meteoblue (EU).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konfigurace měst
# ---------------------------------------------------------------------------

@dataclass
class CityConfig:
    name: str
    country: str
    lat: float
    lon: float
    unit: str          # "F" nebo "C"
    api_source: str    # "NOAA" nebo "METEOBLUE"
    polymarket_name: str  # Název pro hledání v Polymarketu

CITIES: list[CityConfig] = [
    # USA — °F — NOAA
    CityConfig("New York",  "US", 40.7128, -74.0060, "F", "NOAA",      "nyc"),
    CityConfig("Atlanta",   "US", 33.7490, -84.3880, "F", "NOAA",      "atlanta"),
    CityConfig("Chicago",   "US", 41.8781, -87.6298, "F", "NOAA",      "chicago"),
    CityConfig("Miami",     "US", 25.7617, -80.1918, "F", "NOAA",      "miami"),
    CityConfig("Seattle",   "US", 47.6062, -122.3321,"F", "NOAA",      "seattle"),
    CityConfig("Dallas",    "US", 32.7767, -96.7970, "F", "NOAA",      "dallas"),
    # EU — °C — Meteoblue
    CityConfig("London",    "UK", 51.5074,  -0.1278, "C", "METEOBLUE", "london"),
    CityConfig("Paris",     "FR", 48.8566,   2.3522, "C", "METEOBLUE", "paris"),
    CityConfig("Madrid",    "ES", 40.4168,  -3.7038, "C", "METEOBLUE", "madrid"),
    CityConfig("Warsaw",    "PL", 52.2297,  21.0122, "C", "METEOBLUE", "warsaw"),
]

CITY_MAP: dict[str, CityConfig] = {c.name: c for c in CITIES}


# ---------------------------------------------------------------------------
# Datová třída výsledku
# ---------------------------------------------------------------------------

@dataclass
class WeatherForecast:
    city: str
    target_date: date      # datum, pro které předpovídáme
    predicted_high: float  # max. teplota ve správných jednotkách
    unit: str              # "F" nebo "C"
    source: str            # "NOAA" nebo "METEOBLUE"
    raw_celsius: float     # vždy v °C pro interní porovnání
    fetched_at: datetime   # UTC timestamp získání dat

    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "target_date": self.target_date.isoformat(),
            "predicted_high": round(self.predicted_high, 1),
            "unit": self.unit,
            "source": self.source,
            "raw_celsius": round(self.raw_celsius, 1),
            "fetched_at": self.fetched_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Hlavní třída
# ---------------------------------------------------------------------------

class WeatherCollector:
    """
    Sbírá předpovědi počasí z NOAA a Meteoblue.
    Vrací maximální teploty ve správných jednotkách pro Polymarket kontrakty.
    """

    NOAA_BASE = "https://api.weather.gov"
    METEOBLUE_BASE = "https://my.meteoblue.com/packages/basic-day"

    def __init__(self, meteoblue_api_key: str | None = None, timeout: int = 15):
        self.meteoblue_api_key = meteoblue_api_key or os.getenv("METEOBLUE_API_KEY", "")
        self.timeout = timeout
        self._noaa_grid_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Veřejné metody
    # ------------------------------------------------------------------

    def get_all_forecasts(self, target_date: date) -> list[WeatherForecast]:
        """
        Získá předpovědi pro všechna nakonfigurovaná města.
        Tiché selhání pro jednotlivá města (loguje chybu).
        """
        results: list[WeatherForecast] = []
        for city in CITIES:
            try:
                forecast = self.get_forecast(city.name, target_date)
                if forecast:
                    results.append(forecast)
            except Exception as exc:
                logger.error("Chyba předpovědi pro %s: %s", city.name, exc)
        return results

    def get_forecast(self, city_name: str, target_date: date) -> Optional[WeatherForecast]:
        """
        Vrátí předpověď pro konkrétní město a datum.
        """
        city = CITY_MAP.get(city_name)
        if not city:
            raise ValueError(f"Neznámé město: {city_name!r}. Dostupná: {list(CITY_MAP)}")

        if city.api_source == "NOAA":
            return self._fetch_noaa(city, target_date)
        elif city.api_source == "METEOBLUE":
            return self._fetch_meteoblue(city, target_date)
        else:
            raise ValueError(f"Neznámý zdroj: {city.api_source!r}")

    # ------------------------------------------------------------------
    # NOAA (USA)
    # ------------------------------------------------------------------

    def _fetch_noaa(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        NOAA API — dvoustupňová strategie:

        Strategie A (primární): /forecast — 12h periody (isDaytime=True/False)
          Daytime perioda přímo obsahuje "High: 54°F" = to co ukazuje NOAA web
          a co Polymarket používá jako referenci.

        Strategie B (fallback): /forecast/hourly — hodinové periody
          Filtrujeme POUZE hodiny 6:00–18:00 lokálního času (daytime high),
          aby nedošlo k zahrnutí nočního tepla předchozího dne.

        Klíčový poznatek: brát max ze všech 24 hodin je chyba — noční hodiny
        00:00–05:00 bývají teplotně stále součástí předchozího dne.
        NOAA web i Polymarket definují "high" jako maximum přes denní hodiny.
        """
        with httpx.Client(timeout=self.timeout) as client:
            # Krok 1: grid metadata (s cache)
            grid_key = f"{city.lat},{city.lon}"
            if grid_key not in self._noaa_grid_cache:
                url = f"{self.NOAA_BASE}/points/{city.lat:.4f},{city.lon:.4f}"
                resp = client.get(url, headers={"User-Agent": "PolymarketWeatherBot/1.0"})
                resp.raise_for_status()
                self._noaa_grid_cache[grid_key] = resp.json()["properties"]

            props = self._noaa_grid_cache[grid_key]

            # Strategie A: /forecast (12h periody s isDaytime)
            try:
                resp = client.get(
                    props["forecast"],
                    headers={"User-Agent": "PolymarketWeatherBot/1.0"},
                )
                resp.raise_for_status()
                periods_12h: list[dict] = resp.json()["properties"]["periods"]
                high_f = self._daytime_high_from_12h(periods_12h, target_date)
                if high_f is not None:
                    logger.info("NOAA [12h] %s: high=%.0f°F (daytime)", city.name, high_f)
                    return WeatherForecast(
                        city=city.name, target_date=target_date,
                        predicted_high=float(int(high_f + 0.5)),
                        unit="F", source="NOAA",
                        raw_celsius=_f_to_c(high_f),
                        fetched_at=datetime.now(timezone.utc),
                    )
            except Exception as exc:
                logger.warning("NOAA [12h] %s: fallback na hourly (%s)", city.name, exc)

            # Strategie B: /forecast/hourly — pouze hodiny 6–18 lokálního času
            resp = client.get(
                props["forecastHourly"],
                headers={"User-Agent": "PolymarketWeatherBot/1.0"},
            )
            resp.raise_for_status()
            periods_1h: list[dict] = resp.json()["properties"]["periods"]

        high_f = self._daytime_high_from_hourly(periods_1h, target_date)
        if high_f is None:
            logger.warning("NOAA: žádná data pro %s dne %s", city.name, target_date)
            return None

        logger.info("NOAA [hourly] %s: high=%.0f°F (6–18 lokálně)", city.name, high_f)
        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=float(int(high_f + 0.5)),
            unit="F", source="NOAA",
            raw_celsius=_f_to_c(high_f),
            fetched_at=datetime.now(timezone.utc),
        )

    def _daytime_high_from_12h(self, periods: list[dict], target_date: date) -> Optional[float]:
        """
        Z 12h NOAA period vybere daytime periodu pro target_date.
        Perioda s isDaytime=True obsahuje denní maximum přímo.
        """
        for p in periods:
            if not p.get("isDaytime", False):
                continue
            start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
            if start.date() == target_date:
                t = float(p["temperature"])
                if p.get("temperatureUnit", "F") == "C":
                    t = _c_to_f(t)
                return t
        return None

    def _daytime_high_from_hourly(self, periods: list[dict], target_date: date) -> Optional[float]:
        """
        Z hodinových period vybere maximum v 6:00–18:00 lokálního času.
        Ignoruje noční hodiny (0–5, 19–23) které patří teplotně jinému dni.
        """
        temps: list[float] = []
        for p in periods:
            start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
            if start.date() != target_date:
                continue
            local_hour = start.hour  # .hour zachovává lokální offset z ISO stringu
            if not (6 <= local_hour <= 18):
                continue
            t = float(p["temperature"])
            if p.get("temperatureUnit", "F") == "C":
                t = _c_to_f(t)
            temps.append(t)
        return max(temps) if temps else None

    # ------------------------------------------------------------------
    # Meteoblue (EU)
    # ------------------------------------------------------------------

    def _fetch_meteoblue(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        Meteoblue API — balíček basic-day:
        GET https://my.meteoblue.com/packages/basic-day
            ?lat=...&lon=...&apikey=...&format=json&temperature=C
        """
        if not self.meteoblue_api_key:
            raise RuntimeError(
                "METEOBLUE_API_KEY není nastaveno. "
                "Přidej ho do .env nebo jako env proměnnou."
            )

        params = {
            "lat": city.lat,
            "lon": city.lon,
            "apikey": self.meteoblue_api_key,
            "format": "json",
            "temperature": "C",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(self.METEOBLUE_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()

        # Struktura: data["data_day"]["time"] — list datumů
        #            data["data_day"]["temperature_max"] — max. teploty v °C
        try:
            days: list[str] = data["data_day"]["time"]
            temps_max: list[float] = data["data_day"]["temperature_max"]
        except KeyError as exc:
            raise ValueError(f"Meteoblue: neočekávaná struktura odpovědi: {exc}") from exc

        target_str = target_date.isoformat()
        for day_str, temp_max in zip(days, temps_max):
            if day_str == target_str:
                high_c = float(temp_max)
                # EU Polymarket kontrakty: 1 desetinné místo v °C
                high_c_rounded = round(high_c, 1)
                return WeatherForecast(
                    city=city.name,
                    target_date=target_date,
                    predicted_high=high_c_rounded,
                    unit="C",
                    source="METEOBLUE",
                    raw_celsius=high_c,
                    fetched_at=datetime.now(timezone.utc),
                )

        logger.warning("Meteoblue: datum %s nenalezeno pro %s", target_date, city.name)
        return None


# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

def _f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9

def _c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


# ---------------------------------------------------------------------------
# Testovací spuštění
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()

    from datetime import timedelta
    tomorrow = date.today() + timedelta(days=1)
    collector = WeatherCollector()

    print(f"\n=== Předpovědi pro {tomorrow} ===\n")
    for city_cfg in CITIES:
        try:
            fc = collector.get_forecast(city_cfg.name, tomorrow)
            if fc:
                print(f"  {fc.city:12s} → {fc.predicted_high:5.1f}°{fc.unit}  (zdroj: {fc.source})")
        except Exception as e:
            print(f"  {city_cfg.name:12s} → CHYBA: {e}")