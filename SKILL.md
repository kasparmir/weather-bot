---
name: polymarket_weather_bot
description: >
  Autonomní paper-trading bot pro Polymarket Weather Markets.
  Monitoruje předpovědi počasí (NOAA + Meteoblue) pro 10 světových měst,
  nakupuje YES kontrakty na Polymarketu a dynamicky prodává při zisku.
  Používej, když chceš spustit denní nákup, zkontrolovat pozice, zobrazit
  statistiky portfolia nebo diagnostikovat problémy bota.
triggers:
  - "spusť denní nákup"
  - "daily buy"
  - "zkontroluj pozice"
  - "monitor positions"
  - "ukaž portfolio"
  - "weather bot stats"
  - "polymarket weather"
  - "jak se daří botovi"
  - "předpovědi počasí"
  - "weather forecast"
---

# Polymarket Weather Bot — OpenClaw Skill

## Přehled

Tento skill řídí autonomního paper-trading bota, který:
1. **Každý den v 18:00 UTC** získá předpovědi pro 10 měst (6× USA via NOAA, 4× EU via Meteoblue)
2. **Nakoupí YES kontrakty** na Polymarketu pro následující den
3. **Každých 30 minut** monitoruje ceny a prodá při zisku ≥ 50%
4. **Spravuje virtuální portfolio** $1000 USDC (paper trading)

## Konfigurace

**Modely:**
- Orchestrátor: `GLM-5:cloud` (tool-calling, JSON schémata)
- Záložní analýza: `GPT-OSS:120b-cloud` (komplexní market analýza)

**Sledovaná města:**
- USA (°F, NOAA): New York, Atlanta, Chicago, Miami, Seattle, Dallas
- EU (°C, Meteoblue): Londýn, Paříž, Madrid, Varšava

## Workflow (jak skill pracuje)

### Trigger 1: Denní nákup (18:00 UTC)
```
1. weather_api.py → předpovědi pro zítřek
2. polymarket_gamma.py → nalezení kontraktů
3. ledger.py → otevření papírových pozic
4. Výpis výsledků do chatu
```

### Trigger 2: Monitor pozic (každých 30 min)
```
1. ledger.py → načtení OPEN pozic
2. polymarket_gamma.py → aktuální ceny
3. Pokud cena >= 0.50 → zavřít (profit take)
4. Jinak → aktualizovat cenu
```

## Implementace

### Krok 1: Spuštění denního nákupu
```bash
cd /path/to/polymarket-weather-bot
python scripts/daily_buy.py
```

### Krok 2: Kontrola pozic
```bash
python scripts/monitor_positions.py
```

### Krok 3: Dashboard
```bash
streamlit run scripts/dashboard.py --server.port 8501
```

## Cron joby (nastavení)

Spusť `./cron_setup.sh` nebo přidej ručně:

```bash
# Denní nákup — každý den v 18:00 UTC
openclaw cron add \
  --name "Weather Bot: Denní nákup" \
  --cron "0 18 * * *" \
  --tz "UTC" \
  --session isolated \
  --message "Spusť polymarket weather bot denní nákup: python /CESTA/scripts/daily_buy.py a reportuj výsledky" \
  --announce

# Monitor pozic — každých 30 minut
openclaw cron add \
  --name "Weather Bot: Monitor pozic" \
  --cron "*/30 * * * *" \
  --tz "UTC" \
  --session isolated \
  --message "Zkontroluj otevřené pozice weather bota: python /CESTA/scripts/monitor_positions.py" \
  --announce
```

## Diagnostika

### Kontrola API klíčů
```bash
# NOAA — není potřeba API klíč (veřejné)
curl "https://api.weather.gov/points/40.7128,-74.0060"

# Meteoblue — test klíče
python -c "
from scripts.weather_api import WeatherCollector
from datetime import date, timedelta
c = WeatherCollector()
print(c.get_forecast('London', date.today() + timedelta(days=1)))
"

# Polymarket Gamma — bez autentizace
curl "https://gamma-api.polymarket.com/markets?q=temperature+new+york&active=true&limit=5"
```

### Kontrola stavu portfolia
```bash
python -c "
from scripts.ledger import PaperLedger
import json
l = PaperLedger()
print(json.dumps(l.get_stats(), indent=2))
"
```

## Chybové stavy a řešení

| Chyba | Příčina | Řešení |
|-------|---------|--------|
| `METEOBLUE_API_KEY není nastaveno` | Chybí env proměnná | Přidej do .env |
| `Trh nenalezen pro {město}` | Polymarket nemá weather trh | Normální stav, bot pokračuje |
| `Nedostatečná balance` | Portfolio vyčerpáno | Restart s `reset_portfolio.py` |
| `HTTP 404 pro slug` | Špatný formát slugu | Zkontroluj _build_slug() |
| `entry_price mimo rozsah` | Trh neaktivní/nevhodný | Trh přeskočen automaticky |

## Soubory

```
polymarket-weather-bot/
├── SKILL.md                    ← tento soubor
├── HEARTBEAT.md                ← OpenClaw heartbeat instrukce
├── cron_setup.sh               ← automatické nastavení cron jobů
├── .env                        ← API klíče (NEcommitovat!)
├── .env.example                ← šablona
├── requirements.txt
├── scripts/
│   ├── weather_api.py          ← NOAA + Meteoblue sběr dat
│   ├── polymarket_gamma.py     ← Gamma API klient
│   ├── ledger.py               ← paper trading ledger
│   ├── daily_buy.py            ← denní nákupní cyklus
│   ├── monitor_positions.py    ← 30-min monitor
│   └── dashboard.py            ← Streamlit dashboard
└── data/
    ├── trades.csv              ← historie obchodů
    ├── portfolio.json          ← stav portfolia
    └── balance_history.csv     ← vývoj balance
```
