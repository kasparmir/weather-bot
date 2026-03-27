"""
weather_api.py — WeatherCollector
==================================
Providery:
  NOAA          — USA, zdarma, bez API klíče
  WUNDERGROUND  — USA, scraper (bez API klíče)
  OPENMETEO     — USA+EU, zdarma, bez API klíče
  METEOBLUE     — EU, API klíč (METEOBLUE_API_KEY)

Konfigurace providerů — env proměnné:
  USA_WEATHER_PROVIDER=noaa                    # default pro všechna US města
  USA_WEATHER_PROVIDER=wunderground,openmeteo  # WU primárně, open-meteo fallback

Per-město:
  WEATHER_PROVIDER_NEW_YORK=wunderground
  (název UPPERCASE, mezery → _)

Ensemble mód (průměr více zdrojů, doporučeno):
  ENSEMBLE_PROVIDERS=noaa,openmeteo            # US města
  ENSEMBLE_PROVIDERS=noaa,openmeteo,meteoblue  # globálně (meteoblue jen pro EU)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
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
    unit: str
    api_source: str         # default provider
    polymarket_name: str
    wu_slug: str = ""       # Weather Underground URL slug, např. "us/ny/new-york-city"


CITIES: list[CityConfig] = [
    CityConfig("New York", "US", 40.7128, -74.0060, "F", "NOAA", "nyc",
               wu_slug="us/ny/new-york-city"),
    CityConfig("Atlanta",  "US", 33.7490, -84.3880, "F", "NOAA", "atlanta",
               wu_slug="us/ga/atlanta"),
    CityConfig("Chicago",  "US", 41.8781, -87.6298, "F", "NOAA", "chicago",
               wu_slug="us/il/chicago"),
    CityConfig("Miami",    "US", 25.7617, -80.1918, "F", "NOAA", "miami",
               wu_slug="us/fl/miami"),
    CityConfig("Seattle",  "US", 47.6062, -122.3321, "F", "NOAA", "seattle",
               wu_slug="us/wa/seattle"),
    CityConfig("Dallas",   "US", 32.7767, -96.7970, "F", "NOAA", "dallas",
               wu_slug="us/tx/dallas"),
    CityConfig("London",   "UK", 51.5074,  -0.1278, "C", "METEOBLUE", "london"),
    CityConfig("Paris",    "FR", 48.8566,   2.3522, "C", "METEOBLUE", "paris"),
    CityConfig("Madrid",   "ES", 40.4168,  -3.7038, "C", "METEOBLUE", "madrid"),
    CityConfig("Warsaw",   "PL", 52.2297,  21.0122, "C", "METEOBLUE", "warsaw"),
]

CITY_MAP: dict[str, CityConfig] = {c.name: c for c in CITIES}


def _resolve_provider(city: CityConfig) -> list[str]:
    """
    Vrátí seřazený seznam providerů pro město.
    Priorita: per-město env > globální USA env > default z CityConfig.
    """
    env_key = "WEATHER_PROVIDER_" + city.name.upper().replace(" ", "_")
    per_city = os.getenv(env_key, "").strip().lower()
    if per_city:
        return [p.strip() for p in per_city.split(",") if p.strip()]

    if city.country == "US":
        global_usa = os.getenv("USA_WEATHER_PROVIDER", "").strip().lower()
        if global_usa:
            return [p.strip() for p in global_usa.split(",") if p.strip()]

    return [city.api_source.lower()]


# ---------------------------------------------------------------------------
# Datová třída výsledku
# ---------------------------------------------------------------------------

@dataclass
class WeatherForecast:
    city: str
    target_date: date
    predicted_high: float
    unit: str
    source: str                        # provider nebo "ENSEMBLE"
    raw_celsius: float
    fetched_at: datetime
    # Ensemble metadata (vyplněno jen při ensemble módu)
    ensemble_values: list[float] = field(default_factory=list)  # hodnoty od každého providera
    ensemble_sources: list[str]  = field(default_factory=list)  # jejich názvy
    std_dev: float = 0.0               # směrodatná odchylka ensemble (míra nejistoty)

    def to_dict(self) -> dict:
        d = {
            "city": self.city,
            "target_date": self.target_date.isoformat(),
            "predicted_high": round(self.predicted_high, 1),
            "unit": self.unit,
            "source": self.source,
            "raw_celsius": round(self.raw_celsius, 1),
            "fetched_at": self.fetched_at.isoformat(),
        }
        if self.ensemble_values:
            d["ensemble_values"] = [round(v, 1) for v in self.ensemble_values]
            d["ensemble_sources"] = self.ensemble_sources
            d["std_dev"] = round(self.std_dev, 2)
        return d


# ---------------------------------------------------------------------------
# WeatherCollector
# ---------------------------------------------------------------------------

class WeatherCollector:

    NOAA_BASE      = "https://api.weather.gov"
    METEOBLUE_BASE = "https://my.meteoblue.com/packages/basic-day"
    WU_BASE        = "https://www.wunderground.com/forecast"

    # Browser-like headers pro WU scraping
    _WU_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
    }

    def __init__(self, meteoblue_api_key: str | None = None, timeout: int = 20):
        self.meteoblue_api_key = meteoblue_api_key or os.getenv("METEOBLUE_API_KEY", "")
        self.timeout = timeout
        self._noaa_grid_cache: dict[str, dict] = {}
        # Ensemble: seznam providerů přes env ENSEMBLE_PROVIDERS=noaa,openmeteo
        # Prázdné = bez ensemble (použij standardní provider chain)
        self._ensemble_providers = [
            p.strip().lower()
            for p in os.getenv("ENSEMBLE_PROVIDERS", "").split(",")
            if p.strip()
        ]

    # ------------------------------------------------------------------
    # Veřejné metody
    # ------------------------------------------------------------------

    def get_all_forecasts(self, target_date: date) -> list[WeatherForecast]:
        results: list[WeatherForecast] = []
        for city in CITIES:
            try:
                fc = self.get_forecast(city.name, target_date)
                if fc:
                    results.append(fc)
            except Exception as exc:
                logger.error("Chyba předpovědi pro %s: %s", city.name, exc)
        return results

    def get_forecast(self, city_name: str, target_date: date) -> Optional[WeatherForecast]:
        city = CITY_MAP.get(city_name)
        if not city:
            raise ValueError(f"Neznámé město: {city_name!r}. Dostupná: {list(CITY_MAP)}")

        # Ensemble mód: více providerů paralelně → zprůměrovat
        if self._ensemble_providers and city.unit in ("F", "C"):
            return self._get_ensemble_forecast(city, target_date)

        # Standardní mód: první úspěšný provider
        providers = _resolve_provider(city)
        last_exc: Optional[Exception] = None

        for provider in providers:
            try:
                fc = self._fetch_by_provider(provider, city, target_date)
                if fc:
                    return fc
                logger.warning("%s [%s]: žádná data", city.name, provider.upper())
            except Exception as exc:
                logger.warning("%s [%s]: %s", city.name, provider.upper(), exc)
                last_exc = exc

        if last_exc:
            raise last_exc
        return None

    def _get_ensemble_forecast(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        Ensemble forecast: sběr předpovědí od všech ENSEMBLE_PROVIDERS,
        výsledek = průměr (nebo medián při velkém rozptylu).

        Logika:
          1. Dotaž se každého providera nezávisle.
          2. Odfiltruj outliers (hodnoty > 2σ od průměru).
          3. Výsledná předpověď = zaokrouhlený průměr zbývajících.
          4. std_dev = míra nejistoty (vysoký → menší pozice v edge filteru).
        """
        import statistics

        values: list[float] = []
        sources: list[str] = []

        for provider in self._ensemble_providers:
            try:
                fc = self._fetch_by_provider(provider, city, target_date)
                if fc:
                    values.append(fc.predicted_high)
                    sources.append(fc.source)
                    logger.info("Ensemble %s [%s]: %.1f°%s",
                                city.name, provider.upper(), fc.predicted_high, city.unit)
            except Exception as exc:
                logger.warning("Ensemble %s [%s]: %s", city.name, provider.upper(), exc)

        if not values:
            return None

        # Outlier removal při 3+ zdrojích (median-based + absolutní limit)
        # 2σ nestačí při malém počtu zdrojů → použijeme medián + pevný práh
        # Práh: 10°F (≈5.5°C) — předpovědi se reálně nerozcházejí víc
        if len(values) >= 3:
            import statistics as _stats
            med = _stats.median(values)
            max_dev = 10.0 if city.unit == "F" else 5.5
            filtered = [(v, s) for v, s in zip(values, sources)
                        if abs(v - med) <= max_dev]
            if filtered and len(filtered) >= 2:
                values, sources = zip(*filtered)  # type: ignore
                values, sources = list(values), list(sources)
                if len(values) < len(filtered) + 1:  # byl odfiltrován alespoň 1
                    logger.info("Ensemble %s: outlier odfiltrován, zbývá %d zdrojů",
                                city.name, len(values))

        mean_val = statistics.mean(values)
        std = statistics.stdev(values) if len(values) > 1 else 0.0

        # Zaokrouhlení stejně jako jednotlivé providery
        if city.unit == "F":
            predicted = float(int(mean_val + 0.5))
        else:
            predicted = round(mean_val, 1)

        raw_c = _f_to_c(mean_val) if city.unit == "F" else mean_val
        src_label = "+".join(sources)

        logger.info(
            "Ensemble %s: high=%.1f°%s (σ=%.1f) z [%s]",
            city.name, predicted, city.unit, std,
            ", ".join(f"{s}={v:.1f}" for s, v in zip(sources, values)),
        )

        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=predicted,
            unit=city.unit, source=f"ENSEMBLE({src_label})",
            raw_celsius=raw_c,
            fetched_at=datetime.now(timezone.utc),
            ensemble_values=list(values),
            ensemble_sources=list(sources),
            std_dev=std,
        )

    def _fetch_by_provider(self, provider: str, city: CityConfig,
                           target_date: date) -> Optional[WeatherForecast]:
        if provider == "noaa":
            return self._fetch_noaa(city, target_date)
        elif provider in ("wunderground", "wu"):
            return self._fetch_wunderground(city, target_date)
        elif provider in ("openmeteo", "open-meteo", "open_meteo"):
            return self._fetch_openmeteo(city, target_date)
        elif provider == "meteoblue":
            return self._fetch_meteoblue(city, target_date)
        raise ValueError(f"Neznámý provider: {provider!r}")

    # ------------------------------------------------------------------
    # NOAA
    # ------------------------------------------------------------------

    def _fetch_noaa(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        Používá /forecast/hourly endpoint.
        Bere maximum ze všech hodinových hodnot pro daný den (lokální čas).
        """
        with httpx.Client(timeout=self.timeout) as client:
            grid_key = f"{city.lat},{city.lon}"
            if grid_key not in self._noaa_grid_cache:
                url = f"{self.NOAA_BASE}/points/{city.lat:.4f},{city.lon:.4f}"
                resp = client.get(url, headers={"User-Agent": "PolymarketWeatherBot/1.0"})
                resp.raise_for_status()
                self._noaa_grid_cache[grid_key] = resp.json()["properties"]

            props = self._noaa_grid_cache[grid_key]
            resp = client.get(props["forecastHourly"],
                              headers={"User-Agent": "PolymarketWeatherBot/1.0"})
            resp.raise_for_status()
            periods: list[dict] = resp.json()["properties"]["periods"]

        temps: list[float] = []
        for p in periods:
            start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
            if start.date() != target_date:
                continue
            t = float(p["temperature"])
            if p.get("temperatureUnit", "F") == "C":
                t = _c_to_f(t)
            temps.append(t)

        if not temps:
            logger.warning("NOAA: žádná data pro %s %s", city.name, target_date)
            return None

        high_f = max(temps)
        logger.info("NOAA %s: high=%d°F (max z %d hodin)", city.name, int(high_f + 0.5), len(temps))
        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=float(int(high_f + 0.5)),
            unit="F", source="NOAA",
            raw_celsius=_f_to_c(high_f),
            fetched_at=datetime.now(timezone.utc),
        )

    # ------------------------------------------------------------------
    # Weather Underground (scraper)
    # ------------------------------------------------------------------

    def _fetch_wunderground(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        Scraper pro wunderground.com/forecast/{wu_slug}.
        Zkouší dvě URL varianty. Detekuje captchu/blokování.
        Při selhání vyhodí RuntimeError → caller přejde na next provider.
        """
        if not city.wu_slug:
            raise ValueError(f"wu_slug není nastaven pro {city.name}")

        urls = [
            f"{self.WU_BASE}/{city.wu_slug}",
            f"https://www.wunderground.com/hourly/{city.wu_slug}",
        ]
        html: Optional[str] = None
        used_url = urls[0]
        last_err: Optional[Exception] = None

        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            for url in urls:
                try:
                    resp = client.get(url, headers=self._WU_HEADERS)
                    if resp.status_code != 200:
                        logger.debug("WU %s: HTTP %d", city.name, resp.status_code)
                        continue
                    body = resp.text
                    if len(body) < 5000 or "__NEXT_DATA__" not in body:
                        logger.debug("WU %s: stránka neobsahuje data (%d B) — možná captcha",
                                     city.name, len(body))
                        continue
                    html = body
                    used_url = url
                    break
                except Exception as exc:
                    last_err = exc
                    logger.debug("WU %s %s: %s", city.name, url, exc)

        if not html:
            raise RuntimeError(
                f"WU: nepodařilo se načíst data pro {city.name} "
                f"(captcha / blokováno). Použij OPENMETEO jako fallback."
                + (f" Poslední chyba: {last_err}" if last_err else "")
            )

        data = self._wu_extract_next_data(html)
        if not data:
            raise RuntimeError(f"WU __NEXT_DATA__ nenalezen ({used_url})")

        high_f = self._wu_parse_daily_high(data, target_date)
        if high_f is None:
            raise RuntimeError(f"WU: high pro {target_date} nenalezen ({city.name})")

        logger.info("WU %s: high=%d°F", city.name, int(high_f + 0.5))
        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=float(int(high_f + 0.5)),
            unit="F", source="WUNDERGROUND",
            raw_celsius=_f_to_c(high_f),
            fetched_at=datetime.now(timezone.utc),
        )

    def _wu_extract_next_data(self, html: str) -> Optional[dict]:
        """
        Extrahuje JSON z <script id="__NEXT_DATA__">.
        Nepoužívá .*? přes celé HTML — najde tag a parsuje od první {.
        """
        tag_pos = html.find('id="__NEXT_DATA__"')
        if tag_pos == -1:
            tag_pos = html.find("id='__NEXT_DATA__'")
        if tag_pos == -1:
            return None
        gt_pos = html.find(">", tag_pos)
        if gt_pos == -1:
            return None
        json_start = html.find("{", gt_pos)
        if json_start == -1:
            return None
        script_end = html.find("</script>", json_start)
        if script_end == -1:
            return None
        json_str = html[json_start:script_end].strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as exc:
            logger.error("WU JSON parse: %s (len=%d)", exc, len(json_str))
            return None

    # ------------------------------------------------------------------
    # Open-Meteo (zdarma, bez API klíče)
    # ------------------------------------------------------------------

    def _fetch_openmeteo(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        Open-Meteo API — https://open-meteo.com
        Zdarma, bez registrace. Vrací hodinové teploty v lokálním čase.
        Bere maximum ze všech hodin daného dne.
        """
        unit_param = "fahrenheit" if city.unit == "F" else "celsius"
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "hourly": "temperature_2m",
            "temperature_unit": unit_param,
            "timezone": "auto",
            "forecast_days": 7,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get("https://api.open-meteo.com/v1/forecast", params=params)
            resp.raise_for_status()
            data = resp.json()

        times: list[str]   = data["hourly"]["time"]
        temps: list[float] = data["hourly"]["temperature_2m"]
        target_str = target_date.isoformat()

        day_temps = [t for ts, t in zip(times, temps)
                     if ts.startswith(target_str) and t is not None]

        if not day_temps:
            logger.warning("OpenMeteo: žádná data pro %s %s", city.name, target_date)
            return None

        high = max(day_temps)
        if city.unit == "F":
            predicted = float(int(high + 0.5))
            raw_c = _f_to_c(high)
        else:
            predicted = round(high, 1)
            raw_c = high

        logger.info("OpenMeteo %s: high=%.1f°%s (max z %d hodin)",
                    city.name, predicted, city.unit, len(day_temps))
        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=predicted,
            unit=city.unit, source="OPENMETEO",
            raw_celsius=raw_c,
            fetched_at=datetime.now(timezone.utc),
        )

    def _wu_parse_daily_high(self, data: dict, target_date: date) -> Optional[float]:
        """
        Extrahuje denní maximum z __NEXT_DATA__.
        Zkouší 4 varianty struktury (WU schéma se mění).

        Struktura Varianta A (nejčastější):
          data.props.pageProps.forecastData.temperatureMax[i]
          data.props.pageProps.forecastData.validTimeLocal[i]

        Varianta B (daypart):
          data.props.pageProps.forecast.daypart[0].temperature[i]
          data.props.pageProps.forecast.validTimeLocal[i]

        Varianta C (SunV3 / novější):
          data.props.pageProps.{anyKey}.calendarDayTemperatureMax[i]
          data.props.pageProps.{anyKey}.validTimeLocal[i]

        Varianta D: regex fallback
        """
        target_str = target_date.isoformat()

        # Pomocná funkce: projde páry (čas, teplota) a najde shodu
        def match_day(times: list, temps: list) -> Optional[float]:
            for t_str, temp in zip(times, temps):
                if temp is None:
                    continue
                day = str(t_str)[:10]
                if day == target_str:
                    return float(temp)
            return None

        pp = data.get("props", {}).get("pageProps", {})

        # --- Varianta A ---
        try:
            fd = pp["forecastData"]
            times = fd.get("validTimeLocal") or fd.get("validTime") or []
            temps = fd.get("temperatureMax") or fd.get("temperature") or []
            result = match_day(times, temps)
            if result is not None:
                return result
        except (KeyError, TypeError):
            pass

        # --- Varianta B ---
        # validTimeLocal má 1 položku na den (3 nebo 7 dní).
        # daypart[0].temperature má 2 položky na den: [den0, noc0, den1, noc1, ...]
        # Denní high je na indexu i*2, noční na i*2+1.
        try:
            fc = pp["forecast"]
            times = fc.get("validTimeLocal") or []
            dp = fc["daypart"][0]
            temps = dp.get("temperature") or []
            for i, t_str in enumerate(times):
                day = str(t_str)[:10] if t_str else ""
                if day == target_str:
                    day_idx = i * 2  # denní index
                    if day_idx < len(temps) and temps[day_idx] is not None:
                        return float(temps[day_idx])
        except (KeyError, TypeError, IndexError):
            pass

        # --- Varianta C: hledej v jakémkoliv klíči pageProps ---
        for key, obj in pp.items():
            if not isinstance(obj, dict):
                continue
            times = obj.get("validTimeLocal") or obj.get("validTimeUtc") or []
            temps = (obj.get("calendarDayTemperatureMax")
                     or obj.get("temperatureMax")
                     or obj.get("temperature") or [])
            if not times or not temps:
                continue
            result = match_day(times, temps)
            if result is not None:
                logger.debug("WU varianta C (key=%s): %.0f°F", key, result)
                return result

        # --- Varianta D: regex fallback v celém JSON stringu ---
        raw = json.dumps(data)
        pos = raw.find(target_str)
        if pos > 0:
            window = raw[max(0, pos - 3000): pos + 500]
            for pattern in [
                r'"temperatureMax"\s*:\s*\[([^\]]+)\]',
                r'"calendarDayTemperatureMax"\s*:\s*\[([^\]]+)\]',
                r'"temperature"\s*:\s*\[([^\]]+)\]',
            ]:
                m = re.search(pattern, window)
                if m:
                    nums = re.findall(r'\b(\d{2,3})\b', m.group(1))
                    if nums:
                        logger.debug("WU varianta D (regex): %s°F", nums[0])
                        return float(nums[0])

        return None

    # ------------------------------------------------------------------
    # Meteoblue (EU)
    # ------------------------------------------------------------------

    def _fetch_meteoblue(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        if not self.meteoblue_api_key:
            raise RuntimeError("METEOBLUE_API_KEY není nastaveno.")

        params = {
            "lat": city.lat, "lon": city.lon,
            "apikey": self.meteoblue_api_key,
            "format": "json", "temperature": "C",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(self.METEOBLUE_BASE, params=params)
            resp.raise_for_status()
            data = resp.json()

        try:
            days: list[str]    = data["data_day"]["time"]
            temps: list[float] = data["data_day"]["temperature_max"]
        except KeyError as exc:
            raise ValueError(f"Meteoblue: neočekávaná struktura: {exc}") from exc

        for day_str, temp_max in zip(days, temps):
            if day_str == target_date.isoformat():
                high_c = float(temp_max)
                logger.info("Meteoblue %s: high=%.1f°C", city.name, high_c)
                return WeatherForecast(
                    city=city.name, target_date=target_date,
                    predicted_high=round(high_c, 1),
                    unit="C", source="METEOBLUE",
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
# CLI test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta
    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    load_dotenv()

    tomorrow = date.today() + timedelta(days=1)
    collector = WeatherCollector()

    print(f"\n=== Předpovědi pro {tomorrow} ===\n")
    print(f"  {'Město':12s}  {'Provider':20s}  {'Teplota':>8s}  Zdroj")
    print("  " + "-" * 55)

    for city_cfg in CITIES:
        providers = _resolve_provider(city_cfg)
        prov_str = " → ".join(p.upper() for p in providers)
        try:
            fc = collector.get_forecast(city_cfg.name, tomorrow)
            if fc:
                print(f"  {fc.city:12s}  [{prov_str:18s}]  "
                      f"{fc.predicted_high:5.1f}°{fc.unit}  [{fc.source}]")
            else:
                print(f"  {city_cfg.name:12s}  [{prov_str:18s}]  — žádná data")
        except Exception as e:
            print(f"  {city_cfg.name:12s}  [{prov_str:18s}]  CHYBA: {e}")