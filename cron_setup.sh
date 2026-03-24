#!/usr/bin/env bash
# cron_setup.sh — Nastavení OpenClaw cron jobů pro Polymarket Weather Bot
#
# Použití:
#   chmod +x cron_setup.sh
#   ./cron_setup.sh
#
# Předpoklady:
#   - OpenClaw je nainstalován a nakonfigurován
#   - `openclaw` příkaz je dostupný v PATH

set -euo pipefail

# ---------------------------------------------------------------------------
# Konfigurace — uprav tyto proměnné!
# ---------------------------------------------------------------------------

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_CMD="${PYTHON_CMD:-python3}"
OPENCLAW_SESSION="${OPENCLAW_SESSION:-main}"
NOTIFY_CHANNEL="${NOTIFY_CHANNEL:-}"    # např. "telegram" nebo prázdné = bez notifikace
NOTIFY_TARGET="${NOTIFY_TARGET:-}"      # např. Chat ID pro Telegram

# ---------------------------------------------------------------------------
# Pomocné funkce
# ---------------------------------------------------------------------------

log() { echo "[cron_setup] $*"; }
warn() { echo "[cron_setup] ⚠️  $*" >&2; }

check_openclaw() {
    if ! command -v openclaw &> /dev/null; then
        warn "OpenClaw není v PATH. Nainstaluj ho: npm install -g openclaw@latest"
        warn "Nebo nastav PATH ručně a spusť znovu."
        exit 1
    fi
    log "✓ OpenClaw nalezen: $(openclaw --version 2>/dev/null || echo 'verze neznámá')"
}

build_delivery_args() {
    if [[ -n "$NOTIFY_CHANNEL" && -n "$NOTIFY_TARGET" ]]; then
        echo "--announce --channel $NOTIFY_CHANNEL --to $NOTIFY_TARGET"
    elif [[ -n "$NOTIFY_CHANNEL" ]]; then
        echo "--announce --channel $NOTIFY_CHANNEL"
    else
        echo "--announce"
    fi
}

# ---------------------------------------------------------------------------
# Nastavení cron jobů
# ---------------------------------------------------------------------------

setup_daily_buy() {
    log "Nastavuji cron job: Denní nákup (18:00 UTC)..."

    local delivery_args
    delivery_args=$(build_delivery_args)

    # Odstraň existující job se stejným názvem (ignoruj chybu pokud neexistuje)
    openclaw cron list 2>/dev/null | grep -q "Weather Bot: Denní nákup" && \
        openclaw cron list 2>/dev/null | \
        python3 -c "
import sys, json
jobs = json.load(sys.stdin) if sys.stdin.read().strip().startswith('[') else []
" 2>/dev/null || true

    openclaw cron add \
        --name "Weather Bot: Denní nákup" \
        --cron "0 18 * * *" \
        --tz "UTC" \
        --session "cron:weather-daily-buy" \
        --message "Spusť polymarket weather bot denní nákup. Příkaz: cd ${BOT_DIR} && ${PYTHON_CMD} scripts/daily_buy.py. Reportuj výsledky: počet otevřených pozic, nová balance, případné chyby." \
        $delivery_args \
        --best-effort

    log "✓ Denní nákup nastaven: každý den v 18:00 UTC"
}

setup_monitor() {
    log "Nastavuji cron job: Monitor pozic (každých 30 minut)..."

    local delivery_args
    delivery_args=$(build_delivery_args)

    openclaw cron add \
        --name "Weather Bot: Monitor pozic" \
        --cron "*/30 * * * *" \
        --tz "UTC" \
        --session "cron:weather-monitor" \
        --message "Zkontroluj otevřené pozice polymarket weather bota. Příkaz: cd ${BOT_DIR} && ${PYTHON_CMD} scripts/monitor_positions.py. Pokud výstup obsahuje HEARTBEAT_OK, neposílej zprávu. Pokud obsahuje PROFIT TAKE, notifikuj ihned." \
        $delivery_args \
        --best-effort

    log "✓ Monitor pozic nastaven: každých 30 minut"
}

setup_morning_report() {
    log "Nastavuji cron job: Ranní report (08:00 UTC)..."

    local delivery_args
    delivery_args=$(build_delivery_args)

    openclaw cron add \
        --name "Weather Bot: Ranní report" \
        --cron "0 8 * * *" \
        --tz "UTC" \
        --session "cron:weather-morning" \
        --message "Zobraz ranní report polymarket weather bota. Příkaz: cd ${BOT_DIR} && ${PYTHON_CMD} -c \"from scripts.ledger import PaperLedger; import json; l = PaperLedger(); stats = l.get_stats(); print(json.dumps(stats, indent=2))\". Shrň: balance, otevřené pozice, win rate, připomeň dnešní nákup v 18:00 UTC." \
        $delivery_args \
        --best-effort

    log "✓ Ranní report nastaven: každý den v 08:00 UTC"
}

# ---------------------------------------------------------------------------
# Alternativa: přímé cron joby (pokud OpenClaw cron nefunguje)
# ---------------------------------------------------------------------------

setup_system_cron_fallback() {
    log ""
    log "=== ALTERNATIVA: Systémové cron joby (bez OpenClaw) ==="
    log ""
    log "Pokud OpenClaw cron nefunguje, přidej tyto řádky do crontab (crontab -e):"
    log ""
    cat << EOF
# Polymarket Weather Bot
# Denní nákup — 18:00 UTC
0 18 * * * cd ${BOT_DIR} && ${PYTHON_CMD} scripts/daily_buy.py >> logs/daily_buy.log 2>&1

# Monitor pozic — každých 30 minut
*/30 * * * * cd ${BOT_DIR} && ${PYTHON_CMD} scripts/monitor_positions.py >> logs/monitor.log 2>&1

# Ranní report — 08:00 UTC
0 8 * * * cd ${BOT_DIR} && ${PYTHON_CMD} -c "from scripts.ledger import PaperLedger; import json; l = PaperLedger(); print(json.dumps(l.get_stats(), indent=2))" >> logs/morning_report.log 2>&1
EOF
    log ""
}

# ---------------------------------------------------------------------------
# Generování crons.json (pro ruční import do OpenClaw)
# ---------------------------------------------------------------------------

generate_crons_json() {
    local output="${BOT_DIR}/crons.json"
    log "Generuji ${output}..."

    cat > "$output" << EOF
{
  "version": 1,
  "jobs": [
    {
      "id": "weather-daily-buy",
      "agentId": "main",
      "name": "Weather Bot: Denní nákup",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "expr": "0 18 * * *",
        "tz": "UTC"
      },
      "sessionTarget": "isolated",
      "wakeMode": "next-heartbeat",
      "payload": {
        "kind": "agentTurn",
        "message": "Spusť polymarket weather bot denní nákup. Příkaz: cd ${BOT_DIR} && ${PYTHON_CMD} scripts/daily_buy.py. Reportuj výsledky.",
        "timeoutSeconds": 300,
        "model": "GLM-5:cloud"
      },
      "delivery": {
        "mode": "announce",
        "bestEffort": true
      }
    },
    {
      "id": "weather-monitor",
      "agentId": "main",
      "name": "Weather Bot: Monitor pozic",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "expr": "*/30 * * * *",
        "tz": "UTC"
      },
      "sessionTarget": "isolated",
      "wakeMode": "next-heartbeat",
      "payload": {
        "kind": "agentTurn",
        "message": "Zkontroluj otevřené pozice weather bota: cd ${BOT_DIR} && ${PYTHON_CMD} scripts/monitor_positions.py. Pokud HEARTBEAT_OK, neposílej zprávu.",
        "timeoutSeconds": 120,
        "model": "GLM-5:cloud"
      },
      "delivery": {
        "mode": "announce",
        "bestEffort": true
      }
    },
    {
      "id": "weather-morning",
      "agentId": "main",
      "name": "Weather Bot: Ranní report",
      "enabled": true,
      "schedule": {
        "kind": "cron",
        "expr": "0 8 * * *",
        "tz": "UTC"
      },
      "sessionTarget": "isolated",
      "wakeMode": "next-heartbeat",
      "payload": {
        "kind": "agentTurn",
        "message": "Ranní report weather bota: cd ${BOT_DIR} && ${PYTHON_CMD} -c \"from scripts.ledger import PaperLedger; import json; l = PaperLedger(); print(json.dumps(l.get_stats(), indent=2))\"",
        "timeoutSeconds": 60,
        "model": "GLM-5:cloud"
      },
      "delivery": {
        "mode": "announce",
        "bestEffort": true
      }
    }
  ]
}
EOF
    log "✓ ${output} vygenerován"
    log ""
    log "Pro ruční import: zkopíruj obsah do ~/.openclaw/cron/jobs.json"
}

# ---------------------------------------------------------------------------
# Ověření instalace
# ---------------------------------------------------------------------------

verify_setup() {
    log ""
    log "=== Ověření instalace ==="

    # Test Python a dependencies
    if $PYTHON_CMD -c "import httpx, dotenv" 2>/dev/null; then
        log "✓ Python dependencies OK"
    else
        warn "Chybí Python balíčky. Spusť: pip install -r requirements.txt"
    fi

    # Test .env souboru
    if [[ -f "${BOT_DIR}/.env" ]]; then
        log "✓ .env soubor nalezen"
        if grep -q "METEOBLUE_API_KEY=" "${BOT_DIR}/.env" 2>/dev/null; then
            log "✓ METEOBLUE_API_KEY nastaven"
        else
            warn "METEOBLUE_API_KEY chybí v .env (potřeba pro EU města)"
        fi
    else
        warn ".env soubor neexistuje. Zkopíruj: cp .env.example .env"
    fi

    # Test data adresáře
    mkdir -p "${BOT_DIR}/data" "${BOT_DIR}/logs"
    log "✓ Data a logs adresáře připraveny"

    log ""
    log "=== Nastavení dokončeno ==="
    log ""
    log "Příkazy pro ruční test:"
    log "  ${PYTHON_CMD} ${BOT_DIR}/scripts/daily_buy.py"
    log "  ${PYTHON_CMD} ${BOT_DIR}/scripts/monitor_positions.py"
    log "  streamlit run ${BOT_DIR}/scripts/dashboard.py"
    log ""
    log "Zkontroluj cron joby: openclaw cron list"
}

# ---------------------------------------------------------------------------
# Hlavní tok
# ---------------------------------------------------------------------------

main() {
    log "=== Polymarket Weather Bot — Nastavení cron jobů ==="
    log "BOT_DIR: ${BOT_DIR}"
    log ""

    check_openclaw

    log "Nastavuji OpenClaw cron joby..."
    setup_daily_buy
    setup_monitor
    setup_morning_report

    generate_crons_json
    setup_system_cron_fallback
    verify_setup
}

main "$@"
