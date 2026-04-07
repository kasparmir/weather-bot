"""
weather_api.py — WeatherCollector
==================================
Providery:
  NOAA          — USA, zdarma, bez API klíče
  WUNDERGROUND  — USA, scraper (bez API klíče)
  OPENMETEO     — USA+EU, zdarma, bez API klíče
  YR            — EU, zdarma, bez API klíče (MET Norway / yr.no)
  METEOBLUE     — EU, API klíč (METEOBLUE_API_KEY)

Výchozí EU provider: YR (zdarma, spolehlivý)
Výchozí US provider: NOAA

Ensemble (průměr zdrojů, doporučeno):
  ENSEMBLE_PROVIDERS=noaa,openmeteo          # US ensemble
  ENSEMBLE_PROVIDERS=yr,openmeteo            # EU ensemble

Per-město:
  WEATHER_PROVIDER_NEW_YORK=openmeteo
  (název UPPERCASE, mezery → _)
"""

from __future__ import annotations

import logging
import os
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
    timezone: str = "UTC"   # IANA timezone string, např. "America/New_York"


CITIES: list[CityConfig] = [
    CityConfig("New York", "US", 40.7128, -74.0060, "F", "NOAA", "nyc",
               timezone="America/New_York"),
    CityConfig("Atlanta",  "US", 33.7490, -84.3880, "F", "NOAA", "atlanta",
               timezone="America/New_York"),
    CityConfig("Chicago",  "US", 41.8781, -87.6298, "F", "NOAA", "chicago",
               timezone="America/Chicago"),
    CityConfig("Miami",    "US", 25.7617, -80.1918, "F", "NOAA", "miami",
               timezone="America/New_York"),
    CityConfig("Seattle",  "US", 47.6062, -122.3321, "F", "NOAA", "seattle",
               timezone="America/Los_Angeles"),
    CityConfig("Dallas",   "US", 32.7767, -96.7970, "F", "NOAA", "dallas",
               timezone="America/Chicago"),
    CityConfig("London",   "UK", 51.5074,  -0.1278, "C", "YR", "london",
               timezone="Europe/London"),
    CityConfig("Paris",    "FR", 48.8566,   2.3522, "C", "YR", "paris",
               timezone="Europe/Paris"),
    CityConfig("Madrid",   "ES", 40.4168,  -3.7038, "C", "YR", "madrid",
               timezone="Europe/Madrid"),
    CityConfig("Warsaw",   "PL", 52.2297,  21.0122, "C", "YR", "warsaw",
               timezone="Europe/Warsaw"),
]

CITY_MAP: dict[str, CityConfig] = {c.name: c for c in CITIES}


def _resolve_provider(city: CityConfig) -> list[str]:
    """
    Vrátí seřazený seznam providerů pro město.

    Priorita (od nejvyšší):
      1. Per-město env:   WEATHER_PROVIDER_NEW_YORK=openmeteo
      2. Globální region: USA_WEATHER_PROVIDER=noaa,openmeteo
                          EU_WEATHER_PROVIDER=yr,openmeteo
      3. Default z CityConfig (noaa pro US, yr pro EU)
    """
    def _parse(val: str) -> list[str]:
        return [p.strip() for p in val.strip().lower().split(",") if p.strip()]

    # 1. Per-město
    env_key = "WEATHER_PROVIDER_" + city.name.upper().replace(" ", "_")
    per_city = os.getenv(env_key, "")
    if per_city:
        return _parse(per_city)

    # 2. Regionální
    if city.country == "US":
        global_usa = os.getenv("USA_WEATHER_PROVIDER", "")
        if global_usa:
            return _parse(global_usa)
    else:
        global_eu = os.getenv("EU_WEATHER_PROVIDER", "")
        if global_eu:
            return _parse(global_eu)

    # 3. Default z CityConfig
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
    # Deterministický ensemble (průměr modelů)
    ensemble_values: list[float] = field(default_factory=list)  # hodnoty od každého providera
    ensemble_sources: list[str]  = field(default_factory=list)  # jejich názvy
    std_dev: float = 0.0               # směrodatná odchylka (míra nejistoty)
    # Probabilistický ensemble (raw členové z NWP ensemble systémů — 50–160 hodnot)
    # Pokud vyplněno, edge.py počítá empirické P(X>práh) místo Gaussovské aproximace.
    ensemble_members: list[float] = field(default_factory=list)

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
        if self.ensemble_members:
            import statistics as _s
            d["prob_members_count"] = len(self.ensemble_members)
            d["prob_members_std_dev"] = round(_s.stdev(self.ensemble_members), 2) if len(self.ensemble_members) > 1 else 0.0
        return d


# ---------------------------------------------------------------------------
# WeatherCollector
# ---------------------------------------------------------------------------

class WeatherCollector:

    NOAA_BASE      = "https://api.weather.gov"
    METEOBLUE_BASE = "https://my.meteoblue.com/packages/basic-day"

    # Open-Meteo modely pro globální multi-model ensemble (vše zdarma, bez klíče).
    # Pořadí dle obecné kvality: ECMWF IFS je world-class, ostatní doplňují.
    OPENMETEO_MODELS_GLOBAL = [
        "ecmwf_ifs025",
        "gfs_seamless",
        "icon_seamless",
        "gem_seamless",
        "jma_seamless",
        "metno_seamless",
        "bom_access_global",
        "cma_grapes_global",
    ]
    # Vysokorozlišovací regionální modely pro EU (přidávají se k EU městům)
    OPENMETEO_MODELS_EU_EXTRA = [
        "knmi_harmonie_arome_europe",
        "dmi_harmonie_arome_europe",
        "meteofrance_arome_france_hd",
    ]

    # Výchozí ensemble: openmeteo_models (6+ NWP modelů jedním requestem) + NOAA (US) + YR (EU).
    # NOAA selže silently pro EU města a YR pro US — to je v pořádku, ensemble pokračuje.
    DEFAULT_ENSEMBLE = ["openmeteo_models", "noaa", "yr"]

    def __init__(self, meteoblue_api_key: str | None = None, timeout: int = 20):
        self.meteoblue_api_key = meteoblue_api_key or os.getenv("METEOBLUE_API_KEY", "")
        self.timeout = timeout
        self._noaa_grid_cache: dict[str, dict] = {}
        # Ensemble: env override, jinak default (multi-model). Prázdný řetězec NEVYPÍNÁ ensemble —
        # k vypnutí použij ENSEMBLE_PROVIDERS=off (nebo 'none').
        env_val = os.getenv("ENSEMBLE_PROVIDERS", "").strip().lower()
        if env_val in ("off", "none", "disabled"):
            self._ensemble_providers: list[str] = []
        elif env_val:
            self._ensemble_providers = [p.strip() for p in env_val.split(",") if p.strip()]
        else:
            self._ensemble_providers = list(self.DEFAULT_ENSEMBLE)

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
            # Speciální provider: rozbalí se na N modelů jedním Open-Meteo requestem.
            if provider == "openmeteo_models":
                try:
                    model_results = self._fetch_openmeteo_models(city, target_date)
                    for model_name, temp_in_unit in model_results:
                        values.append(temp_in_unit)
                        sources.append(f"OM:{model_name}")
                        logger.info("Ensemble %s [OM:%s]: %.1f°%s",
                                    city.name, model_name, temp_in_unit, city.unit)
                except Exception as exc:
                    logger.warning("Ensemble %s [openmeteo_models]: %s", city.name, exc)
                continue

            try:
                fc = self._fetch_by_provider(provider, city, target_date)
                if fc:
                    # Normalizuj na city.unit — providery mají hardcoded unit (NOAA="F", YR="C").
                    # Bez konverze by se °C a °F mísily ve stejném průměru.
                    if fc.unit != city.unit:
                        if city.unit == "F":
                            val = _c_to_f(fc.predicted_high)
                        else:
                            val = _f_to_c(fc.predicted_high)
                        logger.debug("Ensemble %s [%s]: konverze %.1f°%s → %.1f°%s",
                                     city.name, provider.upper(),
                                     fc.predicted_high, fc.unit, val, city.unit)
                    else:
                        val = fc.predicted_high
                    values.append(val)
                    sources.append(fc.source)
                    logger.info("Ensemble %s [%s]: %.1f°%s",
                                city.name, provider.upper(), val, city.unit)
            except Exception as exc:
                logger.debug("Ensemble %s [%s]: %s", city.name, provider.upper(), exc)

        if not values:
            return None

        # Outlier removal: IQR-based (1.5×IQR pravidlo), funguje i pro malé vzorky.
        # Záložní hard-cap: ±10°F / ±5.5°C od mediánu (předpovědi se reálně nerozcházejí víc).
        if len(values) >= 4:
            sorted_v = sorted(values)
            n = len(sorted_v)
            q1 = sorted_v[n // 4]
            q3 = sorted_v[(3 * n) // 4]
            iqr = q3 - q1
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            med = statistics.median(values)
            hard = 10.0 if city.unit == "F" else 5.5
            filtered = [
                (v, s) for v, s in zip(values, sources)
                if lo <= v <= hi and abs(v - med) <= hard
            ]
            if len(filtered) >= max(3, len(values) - 2):  # ponecháme aspoň 3, max 2 outliers
                if len(filtered) < len(values):
                    logger.info("Ensemble %s: %d outlier(s) odfiltrováno, zbývá %d zdrojů",
                                city.name, len(values) - len(filtered), len(filtered))
                values, sources = [v for v, _ in filtered], [s for _, s in filtered]
        elif len(values) >= 3:
            med = statistics.median(values)
            max_dev = 10.0 if city.unit == "F" else 5.5
            filtered = [(v, s) for v, s in zip(values, sources) if abs(v - med) <= max_dev]
            if len(filtered) >= 2:
                values, sources = [v for v, _ in filtered], [s for _, s in filtered]

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

        # Probabilistický ensemble: 50–160 raw členů z NWP ensemble systémů.
        # Používá se pro empirické P(X > práh) v edge.py — přesnější než Gaussian.
        prob_members: list[float] = []
        try:
            prob_members = self._fetch_openmeteo_probabilistic(city, target_date)
        except Exception as exc:
            logger.warning("OM-probabilistic %s: %s — bude použita Gaussovská aproximace",
                           city.name, exc)

        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=predicted,
            unit=city.unit, source=f"ENSEMBLE({src_label})",
            raw_celsius=raw_c,
            fetched_at=datetime.now(timezone.utc),
            ensemble_values=list(values),
            ensemble_sources=list(sources),
            std_dev=std,
            ensemble_members=prob_members,
        )

    def _fetch_by_provider(self, provider: str, city: CityConfig,
                           target_date: date) -> Optional[WeatherForecast]:
        if provider == "noaa":
            return self._fetch_noaa(city, target_date)
        elif provider in ("openmeteo", "open-meteo", "open_meteo"):
            return self._fetch_openmeteo(city, target_date)
        elif provider in ("yr", "yr.no", "met.no"):
            return self._fetch_yr(city, target_date)
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

    def _fetch_openmeteo_probabilistic(self, city: CityConfig,
                                        target_date: date) -> list[float]:
        """
        Probabilistický ensemble z Open-Meteo Ensemble API.
        Endpoint: https://ensemble-api.open-meteo.com/v1/ensemble

        Poskytuje raw členy (members) ze 4 globálních NWP ensemble systémů — všechno zdarma:
          - ECMWF IFS ENS  (51 členů, world-class)
          - GFS ENS        (31 členů)
          - ICON EPS       (40 členů, evropský)
          - GEM Global EPS (21 členů, kanadský)

        Výsledek: 50–143 float hodnot (teploty v city.unit pro target_date).
        Tato data se předávají do edge.py pro empirické P(X > práh) — bez Gaussovské aproximace.
        """
        import statistics as _s

        # ECMWF IFS "04" = 0.4° grid (globální). GFS "05" = 0.5°. icon_seamless = globální.
        # gem_global má 21 členů — přidáme jako bonus. Vynecháme bom (Australian, méně relevant).
        models = ["ecmwf_ifs04", "gfs05", "icon_seamless", "gem_global"]

        unit_param = "fahrenheit" if city.unit == "F" else "celsius"
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "daily": "temperature_2m_max",
            "temperature_unit": unit_param,
            "timezone": "auto",
            "forecast_days": 7,
            "models": ",".join(models),
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                "https://ensemble-api.open-meteo.com/v1/ensemble",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily") or {}
        days: list[str] = daily.get("time") or []

        if not days:
            return []

        try:
            day_idx = days.index(target_date.isoformat())
        except ValueError:
            logger.warning("OM-probabilistic: target date %s mimo rozsah pro %s",
                           target_date, city.name)
            return []

        members: list[float] = []
        for key, arr in daily.items():
            # Klíče jako "temperature_2m_max_member00", "temperature_2m_max_member01" ...
            if "member" not in key:
                continue
            if not isinstance(arr, list) or day_idx >= len(arr):
                continue
            val = arr[day_idx]
            if val is None:
                continue
            try:
                members.append(float(val))
            except (TypeError, ValueError):
                continue

        if members:
            std = _s.stdev(members) if len(members) > 1 else 0.0
            logger.info(
                "OM-probabilistic %s: %d členů | high=%.1f°%s | σ=%.2f° | rozsah=[%.1f, %.1f]",
                city.name, len(members), _s.mean(members), city.unit,
                std, min(members), max(members),
            )
        else:
            logger.warning("OM-probabilistic %s: žádná data pro %s", city.name, target_date)

        return members

    def _fetch_openmeteo_models(self, city: CityConfig,
                                 target_date: date) -> list[tuple[str, float]]:
        """
        Multi-model ensemble přes Open-Meteo `models=` parametr.
        Jednou HTTP requestu vrátí denní `temperature_2m_max` pro každý model.

        Modely jsou globální NWP systémy (ECMWF IFS, GFS, ICON, GEM, JMA, MET Norway, ...)
        — všechny zdarma, bez API klíče. Pro EU města se přidají regionální HD modely.

        Vrací: list (model_name, temperature_in_city_unit) — jen modely s validní hodnotou.
        """
        models = list(self.OPENMETEO_MODELS_GLOBAL)
        if city.country != "US":
            models += self.OPENMETEO_MODELS_EU_EXTRA

        unit_param = "fahrenheit" if city.unit == "F" else "celsius"
        params = {
            "latitude": city.lat,
            "longitude": city.lon,
            "daily": "temperature_2m_max",
            "temperature_unit": unit_param,
            "timezone": "auto",
            "forecast_days": 7,
            "models": ",".join(models),
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get("https://api.open-meteo.com/v1/forecast", params=params)
            resp.raise_for_status()
            data = resp.json()

        daily = data.get("daily") or {}
        days: list[str] = daily.get("time") or []
        if not days:
            return []

        try:
            day_idx = days.index(target_date.isoformat())
        except ValueError:
            logger.warning("OM-models: target date %s mimo rozsah pro %s",
                           target_date, city.name)
            return []

        results: list[tuple[str, float]] = []
        for model in models:
            key = f"temperature_2m_max_{model}"
            arr = daily.get(key)
            if not arr or day_idx >= len(arr):
                continue
            val = arr[day_idx]
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            # Hodnota je už v requested unitu (city.unit). Pro °F zaokrouhlíme,
            # pro °C necháme s 1 desetinou — průměr to vyhladí.
            if city.unit == "F":
                fval = float(int(fval + 0.5))
            results.append((model, fval))

        if not results:
            logger.warning("OM-models: žádný model nevrátil data pro %s %s",
                           city.name, target_date)
        else:
            logger.info("OM-models %s: %d/%d modelů vrátilo data",
                        city.name, len(results), len(models))
        return results

    # ------------------------------------------------------------------
    # Yr.no / MET Norway (EU, zdarma, bez API klíče)
    # ------------------------------------------------------------------

    def _fetch_yr(self, city: CityConfig, target_date: date) -> Optional[WeatherForecast]:
        """
        Yr.no / MET Norway Locationforecast 2.0 API.
        Dokumentace: https://developer.yr.no/doc/GettingStarted/

        Endpoint: https://api.met.no/weatherapi/locationforecast/2.0/compact
          ?lat={lat}&lon={lon}

        Povinný User-Agent header s kontaktními informacemi.
        Vrací hodinové hodnoty air_temperature v °C.
        Bere maximum za daný den (lokální čas dle offset z API).
        """
        params = {
            "lat": round(city.lat, 4),
            "lon": round(city.lon, 4),
        }
        headers = {
            "User-Agent": "PolymarketWeatherBot/1.0 github.com/polymarket-weather-bot",
            "Accept": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(
                "https://api.met.no/weatherapi/locationforecast/2.0/compact",
                params=params, headers=headers,
            )
            # 203 = beta/deprecated (data jsou stále validní)
            if resp.status_code not in (200, 203):
                resp.raise_for_status()
            data = resp.json()

        # Struktura: data.properties.timeseries[i].time  (ISO UTC)
        #            data.properties.timeseries[i].data.instant.details.air_temperature (°C)
        try:
            timeseries = data["properties"]["timeseries"]
        except KeyError as exc:
            raise ValueError(f"Yr: neočekávaná struktura odpovědi: {exc}") from exc

        target_str = target_date.isoformat()
        temps_c: list[float] = []

        for entry in timeseries:
            t_str = entry.get("time", "")
            # time je UTC ISO string: "2026-03-26T12:00:00Z"
            # Konvertujeme na lokální datum pomocí offset z CityConfig
            try:
                dt_utc = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            except ValueError:
                continue

            # Pro EU města odhadujeme lokální čas podle zeměpisné délky
            # (CET = UTC+1, CEST = UTC+2 — používáme UTC+1 jako konzervativní odhad)
            # Pro přesnost jen filtrujeme UTC datum, protože rozdíl je max 1h
            if dt_utc.date().isoformat() != target_str:
                continue

            try:
                temp = float(
                    entry["data"]["instant"]["details"]["air_temperature"]
                )
                temps_c.append(temp)
            except (KeyError, TypeError, ValueError):
                continue

        if not temps_c:
            logger.warning("Yr: žádná data pro %s %s", city.name, target_date)
            return None

        high_c = max(temps_c)
        logger.info("Yr %s: high=%.1f°C (max z %d hodin)", city.name, high_c, len(temps_c))

        return WeatherForecast(
            city=city.name, target_date=target_date,
            predicted_high=round(high_c, 1),
            unit="C", source="YR",
            raw_celsius=high_c,
            fetched_at=datetime.now(timezone.utc),
        )

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