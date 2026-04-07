"""
edge.py — Edge filter pro Polymarket Weather Bot
=================================================
Rozhoduje, zda vstoupit do pozice na základě rozdílu mezi
naší odhadovanou pravděpodobností výhry a tržní cenou YES.

Logika:
  1. Forecast → pravděpodobnost.
     a) Pokud jsou k dispozici probabilistické ensemble členy (50–160 hodnot
        z ECMWF, GFS, ICON, GEM): empirické P(X > práh) = count / n.
     b) Jinak: Gaussovská aproximace N(predicted, sigma).
  2. Edge = naše_prob - tržní_cena_YES.
  3. Vstoupit pouze pokud Edge >= MIN_EDGE (výchozí 2.5 %) AND P(YES) >= MIN_PROBABILITY.

Konfigurace (env proměnné):
  MIN_EDGE=0.025             # minimální edge pro vstup
  MIN_PROBABILITY=0.30       # minimální P(YES) — zabrání lottery-ticket trhům
  FORECAST_SIGMA_F=2.0       # nejistota (fallback bez ensemble) v °F
  FORECAST_SIGMA_C=1.1       # nejistota (fallback bez ensemble) v °C
  EDGE_ENSEMBLE_SIGMA=true   # použít std_dev z ensemble jako sigma (výchozí true)
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from weather_api import WeatherForecast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------

MIN_EDGE = float(os.getenv("MIN_EDGE", "0.025"))
# Minimální P(YES), aby vůbec stálo za to vstoupit. Brání nákupu lottery-ticket trhů,
# kde je "edge" pozitivní jen proto, že trh sám je 1 % a my říkáme 4 %.
MIN_PROBABILITY = float(os.getenv("MIN_PROBABILITY", "0.30"))
# Výchozí směrodatná odchylka předpovědi (nejistota) — používá se jako spodní hranice;
# multi-model ensemble obvykle dodá vlastní (často nižší) sigma.
# 24h forecast multi-model ensemble: typicky ±1.5–2.5°F / ±0.8–1.4°C
FORECAST_SIGMA_F = float(os.getenv("FORECAST_SIGMA_F", "2.0"))
FORECAST_SIGMA_C = float(os.getenv("FORECAST_SIGMA_C", "1.1"))
# Pokud True, při ensemble použij max(FORECAST_SIGMA, std_dev_ensemble)
USE_ENSEMBLE_SIGMA = os.getenv("EDGE_ENSEMBLE_SIGMA", "true").lower() != "false"


# ---------------------------------------------------------------------------
# Výsledek edge analýzy
# ---------------------------------------------------------------------------

@dataclass
class EdgeResult:
    city: str
    predicted_temp: float
    unit: str
    market_threshold: float     # teplotní práh kontraktu
    market_direction: str       # "above" / "below" / "range" / "unknown"
    our_probability: float      # naše odhadovaná P(YES)  0–1
    market_price: float         # tržní cena YES          0–1
    edge: float                 # our_probability - market_price
    sigma_used: float           # nejistota použitá při výpočtu
    passes: bool                # True → vstoupit
    reason: str                 # lidsky čitelný důvod
    prob_method: str = "gaussian"  # "empirical" | "gaussian"
    prob_members: int = 0          # počet probabilistických členů (0 = Gaussian fallback)

    def log(self) -> None:
        icon = "✅" if self.passes else "⏭️"
        method_str = f"empirical({self.prob_members})" if self.prob_method == "empirical" else f"gaussian(σ={self.sigma_used:.1f}°)"
        logger.info(
            "%s Edge %s: forecast=%.1f°%s | práh=%.1f°%s (%s) | "
            "P(YES)=%.1f%% [%s] | tržní=%.1f%% | edge=%+.1f%% (min %.1f%%)",
            icon, self.city,
            self.predicted_temp, self.unit,
            self.market_threshold, self.unit, self.market_direction,
            self.our_probability * 100,
            method_str,
            self.market_price * 100,
            self.edge * 100,
            MIN_EDGE * 100,
        )


# ---------------------------------------------------------------------------
# Hlavní funkce
# ---------------------------------------------------------------------------

def compute_edge(
    forecast: "WeatherForecast",
    market_threshold: float,
    market_direction: str,
    market_price: float,
) -> EdgeResult:
    """
    Vypočítá edge pro daný kontrakt.

    Parametry:
      forecast          — naše předpověď (s volitelným std_dev z ensemble)
      market_threshold  — teplotní práh kontraktu (extrahovaný ze slugu/otázky)
      market_direction  — "above" | "below" | "range" | "unknown"
      market_price      — aktuální cena YES (0–1)

    Návrat:
      EdgeResult s rozhodnutím passes=True/False
    """
    unit = forecast.unit
    predicted = forecast.predicted_high

    # Sigma pro Gaussovský fallback
    base_sigma = FORECAST_SIGMA_F if unit == "F" else FORECAST_SIGMA_C
    if USE_ENSEMBLE_SIGMA and forecast.std_dev > 0:
        sigma = max(base_sigma, forecast.std_dev)
    else:
        sigma = base_sigma

    # Pravděpodobnost výhry:
    #   Primárně: empirické P z probabilistických ensemble členů (50–160 hodnot).
    #   Fallback: Gaussovská aproximace N(predicted, sigma).
    prob_method = "gaussian"
    prob_members_count = 0
    members = getattr(forecast, "ensemble_members", [])

    if members and len(members) >= 10:
        our_prob = _compute_empirical_probability(members, market_threshold, market_direction)
        prob_method = "empirical"
        prob_members_count = len(members)
    else:
        our_prob = _compute_probability(
            predicted=predicted,
            threshold=market_threshold,
            direction=market_direction,
            sigma=sigma,
        )

    edge = our_prob - market_price
    passes = edge >= MIN_EDGE and our_prob >= MIN_PROBABILITY

    if passes:
        reason = (f"edge {edge*100:+.1f}% >= min {MIN_EDGE*100:.1f}% "
                  f"(P={our_prob*100:.1f}%, tržní={market_price*100:.1f}%, {prob_method})")
    elif our_prob < MIN_PROBABILITY:
        reason = (f"P(YES) {our_prob*100:.1f}% < min {MIN_PROBABILITY*100:.0f}% "
                  f"(edge {edge*100:+.1f}%, {prob_method})")
    else:
        reason = (f"edge {edge*100:+.1f}% < min {MIN_EDGE*100:.1f}% "
                  f"(P={our_prob*100:.1f}%, tržní={market_price*100:.1f}%, {prob_method})")

    return EdgeResult(
        city=forecast.city,
        predicted_temp=predicted,
        unit=unit,
        market_threshold=market_threshold,
        market_direction=market_direction,
        our_probability=our_prob,
        market_price=market_price,
        edge=edge,
        sigma_used=sigma,
        passes=passes,
        reason=reason,
        prob_method=prob_method,
        prob_members=prob_members_count,
    )


def check_edge(
    forecast: "WeatherForecast",
    market_threshold: float,
    market_direction: str,
    market_price: float,
) -> EdgeResult:
    """Syntaktický sugar: vypočítá edge a zaloguje výsledek."""
    result = compute_edge(forecast, market_threshold, market_direction, market_price)
    result.log()
    return result


# ---------------------------------------------------------------------------
# Pravděpodobnostní model
# ---------------------------------------------------------------------------

def _compute_probability(
    predicted: float,
    threshold: float,
    direction: str,
    sigma: float,
) -> float:
    """
    Odhadne P(YES) pomocí normálního rozdělení.

    Model: skutečná teplota ~ N(predicted, sigma)
      - "above" / "exceed": P(X > threshold) = 1 - Φ((threshold - predicted) / sigma)
      - "below":            P(X < threshold) = Φ((threshold - predicted) / sigma)
      - "range" [lo, hi]:   P(lo ≤ X ≤ hi)
      - "unknown":          fallback na abs vzdálenost

    Φ = CDF normálního rozdělení (aproximace bez scipy).
    """
    if sigma <= 0:
        sigma = 1e-6

    if direction == "above":
        # P(actual > threshold)
        z = (threshold - predicted) / sigma
        return 1.0 - _normal_cdf(z)

    elif direction == "below":
        # P(actual < threshold)  — ve skutečnosti "at or below"
        z = (threshold - predicted) / sigma
        return _normal_cdf(z)

    elif direction == "range":
        # threshold je střed rozsahu, šířka ±0.5 (pro celočíselné °F)
        half_width = 0.5
        z_hi = (threshold + half_width - predicted) / sigma
        z_lo = (threshold - half_width - predicted) / sigma
        return _normal_cdf(z_hi) - _normal_cdf(z_lo)

    else:
        # "unknown" — použij vzdálenost jako proxy
        # Čím blíže forecast k prahu, tím blíže 0.50
        z = abs(threshold - predicted) / sigma
        prob = 1.0 - _normal_cdf(z)
        return max(0.05, min(0.95, 0.5 + (0.5 - prob) * math.copysign(1, predicted - threshold)))


def _compute_empirical_probability(
    members: list[float],
    threshold: float,
    direction: str,
) -> float:
    """
    Empirická P(YES) z probabilistického ensemble (50–160 NWP členů).

    Přímý count překračujících členů místo Gaussovské aproximace.
    Výrazně přesnější při asymetrické distribuci nebo ostrém rozdělení.

    Laplaceovo vyhlazení (+1 na každé straně) zabrání P=0% nebo P=100%.

    Range market: šířka odvozena z unit (°F → ±0.5, °C → ±0.25 pro celé stupně;
    pro desetinné prahy se použije přesný match).
    """
    n = len(members)
    if n == 0:
        return 0.5

    if direction == "above":
        count = sum(1 for m in members if m > threshold)
    elif direction == "below":
        count = sum(1 for m in members if m <= threshold)
    elif direction == "range":
        # Šířka: ±0.5° pro celočíselné prahy, ±0.25° pro desetinné
        half = 0.5 if threshold == int(threshold) else 0.3
        count = sum(1 for m in members if threshold - half <= m <= threshold + half)
    else:
        # Unknown: fallback na "above"
        count = sum(1 for m in members if m > threshold)

    # Laplaceovo vyhlazení — zabrání 0 % a 100 % při malém vzorku
    return (count + 1) / (n + 2)


def _normal_cdf(z: float) -> float:
    """
    Aproximace CDF normálního rozdělení bez scipy.
    Přesnost: ≈ 4 desetinná místa — dostatečné pro edge rozhodování.

    Používá Abramowitz & Stegun aproximaci (7.1.26).
    """
    # Zachytit extrémní hodnoty
    if z < -8.0:
        return 0.0
    if z > 8.0:
        return 1.0

    # Počítáme přes erfc pro stabilitu
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Extrakce prahu a směru ze slugu/otázky (pomocná funkce pro daily_buy)
# ---------------------------------------------------------------------------

def extract_market_info(market_slug: str, question: str,
                        unit: str) -> tuple[Optional[float], str]:
    """
    Extrahuje (threshold, direction) z market slugu nebo otázky.
    Vrací (None, "unknown") pokud nelze určit.

    Používá stejnou logiku jako polymarket_gamma._extract_threshold/_detect_direction.
    """
    import re

    ABOVE_KW = ["exceed", "above", "over", "higher", "more than", "greater"]
    BELOW_KW = ["below", "under", "orbelow", "or-below", "or below",
                "less than", "lower", "at most", "atmost"]

    combined = (question + " " + market_slug).lower()

    # Směr
    direction = "unknown"
    for kw in BELOW_KW:
        if kw in combined:
            direction = "below"
            break
    if direction == "unknown":
        for kw in ABOVE_KW:
            if kw in combined:
                direction = "above"
                break

    # Range market
    ul = unit.lower()
    m = re.search(rf"-(\d{{1,3}}(?:\.\d+)?)-(\d{{1,3}}(?:\.\d+)?){ul}(?:-|$|or)",
                  market_slug, re.IGNORECASE)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo < hi:
            return (lo + hi) / 2, "range"

    # Single threshold
    u = unit.upper()
    patterns = [
        rf"(\d+(?:\.\d+)?)[°\s]*{u}",
        rf"(\d+(?:\.\d+)?){ul}",
        rf"exceed[\s-](\d+(?:\.\d+)?)",
        rf"above[\s-](\d+(?:\.\d+)?)",
        rf"below[\s-](\d+(?:\.\d+)?)",
        rf"-(\d+(?:\.\d+)?)[cf](?:-|$|or)",
        rf"-(\d+(?:\.\d+)?)-",
    ]
    for text in [question, market_slug]:
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                return float(match.group(1)), direction

    return None, direction


# ---------------------------------------------------------------------------
# Typ pro Optional import
# ---------------------------------------------------------------------------
from typing import Optional