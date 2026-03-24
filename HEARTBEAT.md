# HEARTBEAT.md — Polymarket Weather Bot

Tento soubor řídí heartbeat chování OpenClaw agenta pro weather bota.

## Heartbeat rutiny

### 1. Monitor pozic (každých 30 minut)
**Kdy:** Vždy, pokud existují otevřené pozice  
**Akce:**
1. Spusť `python scripts/monitor_positions.py`
2. Pokud výstup obsahuje `PROFIT TAKE` → pošli notifikaci do chatu
3. Pokud jsou chyby → loguj a pokus se znovu při dalším heartbeatu
4. Pokud výstup obsahuje `HEARTBEAT_OK` → nic nevypisuj (potlač hluk)

### 2. Denní nákup (18:00 UTC)
**Kdy:** Jednou denně v 18:00 UTC  
**Akce:**
1. Spusť `python scripts/daily_buy.py`
2. Vždy reportuj výsledky (počet otevřených pozic, balance)
3. Pokud `forecasts_fetched == 0` → upozorni na API problém

### 3. Ranní report (08:00 UTC)
**Kdy:** Každé ráno  
**Akce:**
1. Zobraz stav portfolia: `python -c "from scripts.ledger import PaperLedger; import json; l = PaperLedger(); print(json.dumps(l.get_stats(), indent=2))"`
2. Shrň otevřené pozice na dnešní den
3. Připomeň, že dnes v 18:00 UTC proběhne denní nákup

## Pravidla pro hlášení

- **PROFIT TAKE** → vždy notifikuj (důležitá událost)
- **Nový nákup** → vždy reportuj s detaily (město, teplota, cena)
- **Pravidelný monitor bez změn** → HEARTBEAT_OK, neposílej zprávu
- **Chyba API** → reportuj jednou, ne při každém heartbeatu
- **Nízká balance (< $100)** → upozorni jednou denně

## Kontext pro agenta

Tento bot pracuje s **reálnými tržními daty** ale **virtuálními penězi** (paper trading).
Všechny operace jsou simulované — žádné skutečné transakce na Polymarketu neprobíhají.

Vždy používej model `GLM-5:cloud` pro pravidelné heartbeat úkoly.
Přepni na `GPT-OSS:120b-cloud` pouze pokud:
- Cena trhu neočekávaně skočila o >20% během jednoho heartbeatu
- Více jak 3 trhy současně nedosahují ceny (možná systémová chyba)
- Bot reportuje konzistentní ztráty (win rate < 30% po 20+ obchodech)

## Stav sledování

Udržuj v paměti:
- Počet profit-takes dnes: `{count_today_profit_takes}`
- Poslední chyba API: `{last_api_error}`
- Aktuální balance: `{current_balance}`
- Otevřené pozice: `{open_positions_count}`
