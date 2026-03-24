# 🌡️ Polymarket Weather Bot

Autonomní paper-trading bot pro [Polymarket Weather Markets](https://polymarket.com), běžící na frameworku [OpenClaw](https://openclaw.ai/).

## Přehled

Bot každý den v **18:00 UTC** získá předpovědi počasí pro 10 světových měst, najde odpovídající YES kontrakty na Polymarketu a nakoupí je. Každých **30 minut** monitoruje ceny a při dosažení zisku (≥ 50%) automaticky prodá. Vše je **paper trading** — žádné skutečné peníze.

```
                    ┌─────────────────────────────────┐
                    │      OpenClaw Orchestrátor       │
                    │         (GLM-5:cloud)            │
                    └──────────┬──────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
      ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
      │  weather_api │  │ polymarket_  │  │   ledger.py  │
      │  .py         │  │  gamma.py    │  │              │
      │  NOAA+       │  │  Gamma API   │  │  CSV + JSON  │
      │  Meteoblue   │  │  (ceny, trhy)│  │  portfolio   │
      └──────────────┘  └──────────────┘  └──────────────┘
                                                │
                                                ▼
                                        ┌──────────────┐
                                        │ dashboard.py │
                                        │  Streamlit   │
                                        └──────────────┘
```

## Funkce

- 🌍 **10 měst**: New York, Atlanta, Chicago, Miami, Seattle, Dallas + Londýn, Paříž, Madrid, Varšava
- 📡 **Reálná data**: NOAA (USA, bezplatné) + Meteoblue (EU, API klíč)
- 💰 **Paper trading**: Virtuální $1000 USDC portfolio
- 🎯 **Profit take**: Automatický prodej při ceně ≥ 50%
- 📊 **Dashboard**: Streamlit web UI s live daty
- 🤖 **OpenClaw integrace**: Cron joby, heartbeat, notifikace

## Architektura modelů

| Role | Model | Použití |
|------|-------|---------|
| Orchestrátor | `GLM-5:cloud` | Pravidelné cron joby, tool-calling, JSON schémata |
| Záložní analýza | `GPT-OSS:120b-cloud` | Neočekávané výkyvy trhu, komplexní analýza |

## Rychlý start

### 1. Instalace

```bash
git clone <repo>
cd polymarket-weather-bot
pip install -r requirements.txt
cp .env.example .env
```

### 2. Konfigurace

Uprav `.env`:
```bash
METEOBLUE_API_KEY=tvůj_klíč_zde
```

> **Poznámka**: NOAA API a Polymarket Gamma API jsou **bezplatné** a nevyžadují klíče.

### 3. Test komponent

```bash
# Test weather API
python scripts/weather_api.py

# Test Polymarket Gamma API
python scripts/polymarket_gamma.py

# Test ledgeru
python scripts/ledger.py
```

### 4. Spuštění bota

```bash
# Manuální denní nákup
python scripts/daily_buy.py

# Manuální kontrola pozic
python scripts/monitor_positions.py

# Dashboard
streamlit run scripts/dashboard.py
```

### 5. Nastavení OpenClaw cron jobů

```bash
chmod +x cron_setup.sh
./cron_setup.sh
```

Nebo ručně nakopíruj obsah `crons.json` do `~/.openclaw/cron/jobs.json`.

## Struktura projektu

```
polymarket-weather-bot/
├── SKILL.md                    # OpenClaw skill definice
├── HEARTBEAT.md                # Heartbeat instrukce pro agenta
├── cron_setup.sh               # Automatické nastavení cron jobů
├── crons.json                  # OpenClaw cron job konfigurace (generováno)
├── requirements.txt
├── .env.example                # Šablona konfigurace
├── .env                        # ← vytvoř z .env.example (necommitovat!)
├── scripts/
│   ├── weather_api.py          # WeatherCollector (NOAA + Meteoblue)
│   ├── polymarket_gamma.py     # Gamma API klient
│   ├── ledger.py               # Paper trading ledger
│   ├── daily_buy.py            # Denní nákupní cyklus (18:00 UTC)
│   ├── monitor_positions.py    # 30-min monitor pozic
│   └── dashboard.py            # Streamlit dashboard
├── data/                       # Generováno automaticky
│   ├── trades.csv              # Historie obchodů
│   ├── portfolio.json          # Stav portfolia
│   └── balance_history.csv     # Vývoj balance
└── logs/                       # Generováno automaticky
    ├── daily_buy.log
    └── monitor.log
```

## CSV struktura (trades.csv)

| Sloupec | Typ | Popis |
|---------|-----|-------|
| `id` | string | Unikátní ID obchodu (8 znaků) |
| `timestamp` | ISO UTC | Kdy byl záznam vytvořen |
| `city` | string | Město (New York, London, ...) |
| `target_date` | ISO date | Datum obchodovaného kontraktu |
| `predicted_temp` | float | Předpovídaná teplota |
| `unit` | F/C | Jednotka teploty |
| `market_slug` | string | Polymarket slug kontraktu |
| `market_question` | string | Otázka kontraktu |
| `entry_price` | float 0-1 | Nákupní cena YES kontraktu |
| `current_price` | float 0-1 | Aktuální cena |
| `last_checked` | ISO UTC | Poslední kontrola ceny |
| `status` | enum | OPEN / CLOSED_PROFIT / CLOSED_SETTLEMENT / CLOSED_MANUAL |
| `exit_price` | float 0-1 | Prodejní cena (0 pokud OPEN) |
| `pnl` | float | Profit/loss v USD |
| `pnl_pct` | float | Profit/loss v % |
| `notes` | string | Poznámky (důvod uzavření) |

## Obchodní logika

### Nákup (T-1, 18:00 UTC)
1. Získej předpověď maximální teploty pro zítřek
2. Najdi odpovídající Polymarket YES kontrakt
3. Nakup za `bestAsk` cenu (nebo `lastTradePrice` jako fallback)
4. Investuj $10 USDC per pozici
5. Přeskoč pokud: cena mimo [0.02, 0.98] nebo nedostatek balance

### Exit strategie
- **Profit take**: cena ≥ $0.50 → okamžitý prodej
- **Settlement**: Polymarket uzavře trh → zápis výsledku
- **Manual**: Ruční uzavření přes ledger

### Slug lookup strategie
1. Přesný slug: `highest-temperature-in-{city}-on-{month}-{day}-{year}`
2. Fulltextové hledání: `temperature {city} {month} {day}`
3. Fallback: nejaktivnější weather market pro město

## OpenClaw integrace

### Skill použití
```
# V chatu OpenClaw:
"Spusť denní nákup weather bota"
"Zkontroluj otevřené pozice"
"Ukaž statistiky portfolia"
"Jak se daří botovi dnes?"
```

### Cron joby
| Jméno | Čas | Model | Akce |
|-------|-----|-------|------|
| Weather Bot: Denní nákup | 18:00 UTC | GLM-5:cloud | `daily_buy.py` |
| Weather Bot: Monitor pozic | */30 * * * * | GLM-5:cloud | `monitor_positions.py` |
| Weather Bot: Ranní report | 08:00 UTC | GLM-5:cloud | Stats z ledgeru |

## API reference

### NOAA (USA)
```
Base: https://api.weather.gov
Auth: Není potřeba
Limit: Reasonable use (stovky requestů/den OK)

GET /points/{lat},{lon}     → grid metadata
GET /gridpoints/{wfo}/{x},{y}/forecast/hourly → hodinové předpovědi
```

### Meteoblue (EU)
```
Base: https://my.meteoblue.com
Auth: API klíč v query parametru
Limit: Free tier = 1000 requests/měsíc

GET /packages/basic-day?lat=...&lon=...&apikey=...&format=json
```

### Polymarket Gamma API
```
Base: https://gamma-api.polymarket.com
Auth: Není potřeba (read-only)

GET /markets?q={query}&active=true&closed=false
GET /markets/slug/{slug}
GET /markets/{id}
```

## Troubleshooting

### NOAA API vrací 500 / timeout
```bash
# Test přímo
curl "https://api.weather.gov/points/40.7128,-74.0060"
# Pokud nefunguje — NOAA má výpadek, zkus za hodinu
```

### Meteoblue API Key chyba (401)
```bash
# Ověř klíč v .env
grep METEOBLUE_API_KEY .env
# Test
curl "https://my.meteoblue.com/packages/basic-day?lat=51.5&lon=-0.1&apikey=TVUJ_KLIC&format=json"
```

### Polymarket trh nenalezen
```bash
# Manuálně vyhledej na polymarket.com
# Potom test přes curl
curl "https://gamma-api.polymarket.com/markets?q=temperature+new+york&active=true&limit=10" | python -m json.tool
```

### Reset portfolia
```bash
rm data/trades.csv data/portfolio.json data/balance_history.csv
python scripts/ledger.py  # Inicializuje nové soubory
```

## Licence

MIT
