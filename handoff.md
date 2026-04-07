# Polymarket Weather Bot — Předání projektu Claude Code

## Kontext

Autonomní **paper-trading bot** pro [Polymarket Weather Markets](https://polymarket.com).
Každou hodinu zkontroluje časové okno každého města, nakoupí YES kontrakty na předpovědi počasí, monitoruje pozice a automaticky prodává při zisku nebo stop-lossu. Vše je **paper trading** — žádné skutečné peníze.

Projekt běží na **OpenClaw frameworku** v prostředí:
```
/root/.openclaw/workspace/skills/weather-bot/scripts/
```

---

## Struktura projektu

```
polymarket-weather-bot/
├── run.py                        # Standalone runner (hlavní smyčka)
├── .env                          # Konfigurace (z .env.example)
├── .env.example                  # Šablona konfigurace se všemi možnostmi
├── requirements.txt              # Python závislosti
├── SKILL.md                      # OpenClaw skill definice
├── HEARTBEAT.md                  # OpenClaw heartbeat instrukce
├── cron_setup.sh                 # Nastavení OpenClaw cron jobů
└── scripts/
    ├── weather_api.py            # Sběr předpovědí počasí (767 řádků)
    ├── polymarket_gamma.py       # Polymarket Gamma API klient (628 řádků)
    ├── edge.py                   # Edge filter (288 řádků)
    ├── ledger.py                 # Paper trading ledger (569 řádků)
    ├── daily_buy.py              # Nákupní logika (416 řádků)
    ├── monitor_positions.py      # Monitor a exit logika (437 řádků)
    ├── forecast_recheck.py       # 3h recheck předpovědí (298 řádků)
    ├── dashboard.py              # Streamlit dashboard (489 řádků)
    └── reset_portfolio.py        # Reset portfolia se zálohou
```

---

## Architektura a datový tok

```
run.py (hlavní smyčka)
  ├── každé 2 min   → monitor_positions.py
  ├── každou hodinu → daily_buy.py
  └── každé 3 hod   → forecast_recheck.py

daily_buy.py
  ├── _is_in_buy_window()     # timezone check BEZ API
  ├── duplicate check          # BEZ API (all_trades_today)
  ├── weather_api.py           # teprve teď volá API
  ├── polymarket_gamma.py      # najde Polymarket kontrakt
  ├── edge.py                  # zkontroluje edge
  └── ledger.py                # otevře pozici

monitor_positions.py
  ├── polymarket_gamma.get_market_price()
  ├── profit-take (3 podmínky: cena/abs$/pct%)
  ├── stop-loss
  └── ledger.close_position()

forecast_recheck.py
  ├── weather_api.get_forecast()
  ├── porovná s trade.predicted_temp
  └── ledger.mark_forecast_diverged()
```

---

## Moduly — podrobný popis

### `weather_api.py`

**Providery předpovědí:**

| Provider | Region | API klíč | Poznámka |
|----------|--------|----------|----------|
| `noaa` | USA | Ne | `/forecast` endpoint, 12h periody, `isDaytime=True` = daily high |
| `openmeteo` | USA+EU | Ne | Hodinové teploty, `max()` za den |
| `yr` | EU | Ne | MET Norway, povinný User-Agent |
| `wunderground` | USA | Ne | Scraper `__NEXT_DATA__` JSON, 4 varianty struktury |
| `meteoblue` | EU | Ano | `METEOBLUE_API_KEY` |

**Klíčové třídy:**
```python
@dataclass
class CityConfig:
    name: str; country: str; lat: float; lon: float
    unit: str           # "F" nebo "C"
    api_source: str     # výchozí provider
    polymarket_name: str
    wu_slug: str        # Weather Underground slug
    timezone: str       # IANA, např. "America/New_York"

@dataclass
class WeatherForecast:
    city: str; target_date: date
    predicted_high: float; unit: str; source: str
    raw_celsius: float; fetched_at: datetime
    # Ensemble pole:
    ensemble_values: list[float]
    ensemble_sources: list[str]
    std_dev: float      # míra nejistoty → ovlivňuje edge filter
```

**Konfigurace providerů (priorita):**
1. `WEATHER_PROVIDER_{CITY}=...` per-město
2. `USA_WEATHER_PROVIDER=...` nebo `EU_WEATHER_PROVIDER=...`
3. Default z `CityConfig.api_source`

**Ensemble mód:**
```
ENSEMBLE_PROVIDERS=noaa,openmeteo
→ průměr z více zdrojů, outlier removal (medián ±10°F/5.5°C), std_dev → edge sigma
```

**Sledovaná města:**
```
USA (°F, NOAA): New York (nyc), Atlanta, Chicago, Miami, Seattle, Dallas
EU  (°C, YR):  London, Paris, Madrid, Warsaw
```

---

### `polymarket_gamma.py`

Klient pro Polymarket Gamma API (`https://gamma-api.polymarket.com`).
API je veřejné, bez autentizace.

**4 strategie hledání marketu (v pořadí):**
1. Event slug: `highest-temperature-in-{city}-on-{month}-{day}-{year}`
2. Market slug: `will-the-high-temperature-in-{city}-exceed-{t}{u}-on-{month}-{day}`
3. Tag filter: přes weather tag ID
4. Events scan: prochází aktivní eventy

**Kritická validace `_validate_market_date()`:**
- Každý nalezený market projde validací end_date vs target_date
- Tolerance ±1 den (UTC vs lokální čas)
- Diff > 1 den → market odmítnut → zabrání nákupu April 2 místo March 27

**`_closest_to_prediction()`:**
- Range markety (`50-51f`) → přijme pokud forecast leží uvnitř
- Exact match zaokrouhleného čísla
- Nejbližší smysluplný (below/above směr)

---

### `edge.py`

Statistický filtr vstupu. Porovnává naši odhadovanou pravděpodobnost s tržní cenou YES.

**Model:** Předpověď je `X ~ N(predicted, sigma)`, pravděpodobnost výhry z normálního CDF.

```
"exceed 65°F" + forecast=65°F, sigma=3°F:
  P(X > 65) = 1 - Φ(0) = 50%
  market YES = 42%
  edge = 50% - 42% = +8% ≥ MIN_EDGE(3%) → BUY

"exceed 65°F" + forecast=55°F, sigma=3°F:
  P(X > 65) = 1 - Φ(3.33) = 0.04%
  edge ≈ -10% → SKIP
```

**Sigma:** Výchozí 3°F/1.5°C, při ensemble se použije `max(default_sigma, ensemble.std_dev)`.

**Klíčové funkce:**
```python
compute_edge(forecast, market_threshold, market_direction, market_price) → EdgeResult
check_edge(...)  # totéž + zaloguje výsledek
extract_market_info(market_slug, question, unit) → (threshold, direction)
```

---

### `ledger.py`

Paper trading ledger. Ukládá stav do CSV + JSON.

**Datové soubory:**
```
data/trades.csv          # každá pozice = jeden řádek
data/portfolio.json      # balance, wins, losses, total_pnl
data/balance_history.csv # snapshot balance při každé změně
```

**Trade dataclass — důležitá pole:**
```python
@dataclass
class Trade:
    id: str; city: str; target_date: str
    predicted_temp: float; unit: str
    market_slug: str; entry_price: float
    current_price: float; status: TradeStatus
    exit_price: float; pnl: float; pnl_pct: float
    # Forecast recheck pole:
    forecast_diverged: bool        # True = forecast se změnil od vstupu
    latest_forecast_temp: float    # aktuální forecast při posledním rechecku
    forecast_checked_at: str       # ISO UTC timestamp rechecku
```

**Klíčové metody `PaperLedger`:**
```python
open_position(city, target_date, predicted_temp, unit, market_slug, ..., entry_price)
update_position_price(trade_id, current_price)  # + auto profit-take
close_position(trade_id, exit_price, reason, notes)
mark_forecast_diverged(trade_id, latest_forecast_temp, diverged=True)
get_open_trades()
get_all_trades()   # ← důležité pro duplicate check (zahrnuje closed)
get_stats()
```

**Win/loss definice:** `pnl > 0` = win, `pnl <= 0` = loss (včetně breakeven $0).

---

### `daily_buy.py`

**Timezone-aware nákupní okno:**
```python
def _is_in_buy_window(city, now_utc) -> (bool, date | None):
    # Spočítá sekundy do příští půlnoci v lokálním čase města
    # Pokud hours_until_midnight <= BUY_HOURS_BEFORE → v okně
    # target_date = den jehož 00:00 nastane do BUY_HOURS_BEFORE hodin
```

**Pořadí operací (efektivita):**
1. Timezone check — BEZ API volání
2. Duplicate check z `get_all_trades()` — BEZ API (zahrnuje i uzavřené!)
3. `weather_api.get_forecast()` — API volání
4. `gamma.find_weather_market()` — API volání
5. `edge.check_edge()` — výpočet
6. `ledger.open_position()` — zápis

**Entry price filter:** `0.05 < entry_price < 0.85`
(trhy nad 85% jsou téměř vyřešeny, hrozí nákup při settlement ceně)

---

### `monitor_positions.py`

Spouštěn každé 2 minuty. Pro každou OPEN pozici:

**Settlement logika:**
```
trh.closed=True:
  if 0.05 < yes_price < 0.95:
    → "šedá zóna", Polymarket ještě nezveřejnil výsledek
    → jen aktualizuj cenu, počkej
  else:
    → definitivní výsledek (≤0.05 = NO, ≥0.95 = YES)
    → close_position(reason="CLOSED_SETTLEMENT")
```

**Profit-take (3 podmínky, stačí splnit jednu):**
```python
# A) Cena >= threshold AND >= entry * 1.20
price_condition = current_price >= PROFIT_THRESHOLD and current_price >= entry * 1.20

# B) Absolutní $ zisk
abs_condition = unrealized_pnl_dollar >= PROFIT_TAKE_ABS  # výchozí $0.50

# C) Procentuální zisk
pct_condition = unrealized_pnl_pct >= PROFIT_TAKE_PCT * 100  # výchozí 50%
```

**Diverged pozice** (forecast_diverged=True):
```
min_exit = entry_price * (1 + DIVERGED_PROFIT_TAKE_PCT)  # výchozí +50%
→ prodej jen při dostatečném P&L zisku, ne při absolutní ceně
```

**Stop-loss:**
```
stop_price = entry_price * (1 - STOP_LOSS_THRESHOLD)  # výchozí 60%
→ entry=0.35, stop_price=0.14
```

---

### `forecast_recheck.py`

Spouštěn každé 3 hodiny. Pro každou OPEN pozici:
1. Znovu stáhne forecast pro `trade.target_date`
2. Porovná s `trade.predicted_temp`
3. Pokud `|diff| >= threshold` → `mark_forecast_diverged(True)`
4. Pokud diff zpět v toleranci → `mark_forecast_diverged(False)` (reconcile)

Threshold: `FORECAST_DIVERGE_THRESHOLD_F=5.0°F` / `FORECAST_DIVERGE_THRESHOLD_C=2.5°C`

---

### `dashboard.py`

Streamlit web UI. Spuštění:
```bash
streamlit run scripts/dashboard.py
```

**Sekce:**
- Otevřené pozice (live unrealized P&L počítaný jako `POSITION_SIZE * (current/entry - 1)`)
- Balance history (Plotly graf)
- Uzavřené obchody (tabulka s barevným zvýrazněním)
- Přesnost predikcí (P&L per město)

**Sidebar:** wins/losses přepočítává přímo z CSV (ne z portfolio.json) → konzistentní s tabulkou.

---

## Výchozí konfigurace

```python
# Portfolio
INITIAL_BALANCE = 100.0     # $100
POSITION_SIZE   = 4.0       # $4 per pozice

# Nákup
BUY_HOURS_BEFORE = 4        # okno: 4h před půlnocí lokálně

# Edge filter
MIN_EDGE         = 0.03     # 3% minimální edge
FORECAST_SIGMA_F = 3.0      # ±3°F nejistota
FORECAST_SIGMA_C = 1.5      # ±1.5°C nejistota

# Profit-take
PROFIT_TAKE_THRESHOLD = 0.50  # YES cena >= 0.50 AND >= entry*1.20
PROFIT_TAKE_ABS       = 0.50  # absolutní $0.50 zisk
PROFIT_TAKE_PCT       = 0.50  # 50% zisk od entry

# Stop-loss
STOP_LOSS_THRESHOLD   = 0.60  # 60% ztráta od entry

# Forecast recheck
FORECAST_DIVERGE_THRESHOLD_F = 5.0   # °F
FORECAST_DIVERGE_THRESHOLD_C = 2.5   # °C
DIVERGED_PROFIT_TAKE_PCT     = 0.50  # 50% P&L pro diverged exit

# run.py intervaly
MONITOR_INTERVAL_SEC          = 120      # 2 minuty
DAILY_BUY_INTERVAL_SEC        = 3600     # 1 hodina
FORECAST_RECHECK_INTERVAL_SEC = 10800   # 3 hodiny
```

---

## Klíčové opravené bugy (kontext pro Claude Code)

Tyto bugy byly opraveny v průběhu vývoje — jsou zde proto, aby se neopakovaly:

1. **`?q=` endpoint neexistuje** — Gamma API nemá textové vyhledávání; používáme slug/tag/scan strategie.
2. **`bestAsk` vs `outcomePrices[0]`** — entry_price musí být `market.yes_price` (z outcomePrices), ne bestAsk.
3. **Range market slug** — regex `\d{1,3}` zabrání zachycení roku 2026 jako teploty.
4. **NYC polymarket_name** — je `"nyc"`, ne `"new-york"`.
5. **NOAA overnight bleed** — hourly max ze všech 24h zahrnoval noční teploty. Opraveno: `/forecast` endpoint s `isDaytime=True`.
6. **Banker's rounding** — `round()` v Pythonu dělá banker's rounding; používáme `int(x + 0.5)`.
7. **Date validace** — market pro April 2 se nakupoval místo March 27. Opraveno: `_validate_market_date()` s ±1 den tolerancí.
8. **Dashboard P&L** — bylo natvrdo `* 10` místo `* POSITION_SIZE`.
9. **Duplicate rebuy** — po settlement bot znovu nakupoval. Opraveno: duplicate check kontroluje `get_all_trades()` (ne jen open).
10. **Settlement za špatnou cenu** — Polymarket chvíli po uzavření vrací lastTradePrice (0.92), ne výsledek (0 nebo 1). Opraveno: čeká na `price <= 0.05` nebo `price >= 0.95`.
11. **Win/loss nesrovnalost** — sidebar vs tabulka používaly různou definici výhry (`>=0` vs `>0`). Sjednoceno na `pnl > 0`.
12. **EU_WEATHER_PROVIDER** — proměnná existovala v .env.example ale v kódu nebyla implementována.
13. **Entry price 0.92 prošlo filtrem** — limit byl 0.98, opraveno na 0.85.
14. **Buy window nepřesnost** — staré `local_hour >= 20` nahrazeno přesným výpočtem sekund do půlnoci.
15. **Profit-take příliš brzy** — `entry * 1.05` guard bylo příliš volné (entry=0.48 → sell při 0.504). Změněno na `entry * 1.20`.

---

## Navrhnuté ale neimplementované optimalizace

Byly navrženy, zatím neimplementovány — vhodné pro další rozvoj:

- **Kelly criterion position sizing** — dinamická velikost pozice podle edge a pravděpodobnosti
- **Trailing stop-loss** — posunout stop nahoru když cena roste
- **Korelace pozic** — omezit počet pozic ovlivněných stejnou meteorologickou frontou
- **Profit-take ladder** — prodávat po částech (50% pozice při 0.50, zbytek při 0.70...)
- **Forecast accuracy tracking** — zaznamenávat skutečné teploty po settlementu a sledovat přesnost per-město, per-sezóna, per-provider
- **Provider accuracy tracking** — který provider byl nejpřesnější za posledních 30 dní
- **Intraday re-entry** — nakoupit znovu pokud cena klesla a forecast stále platí

---

## Spuštění

```bash
# Instalace
pip install -r requirements.txt
cp .env.example .env
# Vyplň METEOBLUE_API_KEY pokud chceš EU data přes Meteoblue

# Manuální test
python scripts/daily_buy.py
python scripts/monitor_positions.py
python scripts/forecast_recheck.py

# Plný runner
python run.py

# Dashboard
streamlit run scripts/dashboard.py

# Reset portfolia (se zálohou)
python scripts/reset_portfolio.py --confirm
```

---

## Závislosti

```
httpx>=0.27.0       # HTTP klient
python-dotenv>=1.0.0
streamlit>=1.35.0
pandas>=2.1.0
plotly>=5.18.0
```

Žádné externí závislosti pro weather API (NOAA, Open-Meteo, Yr.no jsou zdarma bez klíče).
`zoneinfo` je součástí Python 3.9+ standardní knihovny.

---

## API reference

**NOAA:**
```
GET https://api.weather.gov/points/{lat},{lon}
GET https://api.weather.gov/gridpoints/{wfo}/{x},{y}/forecast
→ isDaytime=True perioda = daily high
```

**Open-Meteo:**
```
GET https://api.open-meteo.com/v1/forecast
  ?hourly=temperature_2m&timezone=auto&forecast_days=7
→ max() za cílový den
```

**Yr.no (MET Norway):**
```
GET https://api.met.no/weatherapi/locationforecast/2.0/compact
  ?lat={lat}&lon={lon}
→ povinný User-Agent header
→ air_temperature v hodinových intervalech, max() za den
```

**Polymarket Gamma API (veřejné, bez autentizace):**
```
GET https://gamma-api.polymarket.com/events/slug/{slug}
GET https://gamma-api.polymarket.com/markets/slug/{slug}
GET https://gamma-api.polymarket.com/events?active=true&closed=false&...
→ outcomePrices[0] = YES cena (authoritative)
→ end_date = datum settlementu
```