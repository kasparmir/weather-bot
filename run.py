#!/usr/bin/env python3
"""
run.py — Standalone runner pro Polymarket Weather Bot
======================================================
Spuštění:
    python run.py                # dashboard na portu 8501
    python run.py --no-dashboard # bez dashboardu
    python run.py --port 8502    # jiný port

Chování:
  - každé 2 minuty  → monitor_positions.py
  - každou hodinu → daily_buy.py (kupuje jen v okně dle timezone)
  - neustále        → streamlit dashboard (volitelné)

Ukončení: Ctrl+C
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------
BOT_DIR     = Path(__file__).parent
SCRIPTS_DIR = BOT_DIR / "scripts"
LOG_DIR     = BOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

MONITOR_INTERVAL_SEC   = 120        # 2 minuty
DAILY_BUY_INTERVAL_SEC = 3600       # každou hodinu
FORECAST_RECHECK_INTERVAL_SEC = 3 * 3600   # každé 3 hodiny
DASHBOARD_PORT       = 8501

PYTHON = sys.executable             # stejný interpret jako runner

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "runner.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("runner")

# ---------------------------------------------------------------------------
# Stav
# ---------------------------------------------------------------------------
_dashboard_proc: subprocess.Popen | None = None
_last_daily_buy: float = 0.0         # monotonic timestamp
_last_forecast_recheck: float = 0.0  # monotonic timestamp
_running = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Spouštění skriptů
# ---------------------------------------------------------------------------

def run_script(script: str, label: str) -> int:
    """Spustí scripts/{script} jako podproces, výstup loguje v reálném čase."""
    path = SCRIPTS_DIR / script
    log.info("▶ Spouštím %s", label)
    try:
        proc = subprocess.Popen(
            [PYTHON, str(path)],
            cwd=str(BOT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"  {line}")
        proc.wait()
        if proc.returncode == 0:
            log.info("✓ %s dokončen (exit 0)", label)
        else:
            log.warning("⚠ %s skončil s kódem %d", label, proc.returncode)
        return proc.returncode
    except Exception as exc:
        log.error("✗ Chyba při spuštění %s: %s", label, exc)
        return -1


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def start_dashboard(port: int) -> None:
    global _dashboard_proc
    if _dashboard_proc and _dashboard_proc.poll() is None:
        return  # už běží

    log.info("🌐 Spouštím dashboard na http://localhost:%d", port)
    try:
        _dashboard_proc = subprocess.Popen(
            [
                PYTHON, "-m", "streamlit", "run",
                str(SCRIPTS_DIR / "dashboard.py"),
                "--server.port", str(port),
                "--server.headless", "true",
                "--server.runOnSave", "false",
            ],
            cwd=str(BOT_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("✓ Dashboard PID %d | http://localhost:%d", _dashboard_proc.pid, port)
    except Exception as exc:
        log.error("✗ Dashboard se nepodařilo spustit: %s", exc)
        _dashboard_proc = None


def ensure_dashboard(port: int) -> None:
    """Restartuje dashboard pokud spadnul."""
    global _dashboard_proc
    if _dashboard_proc is None:
        return
    if _dashboard_proc.poll() is not None:
        log.warning("⚠ Dashboard se ukončil (kód %d), restartuji…", _dashboard_proc.returncode)
        start_dashboard(port)


def stop_dashboard() -> None:
    global _dashboard_proc
    if _dashboard_proc and _dashboard_proc.poll() is None:
        log.info("Ukončuji dashboard (PID %d)…", _dashboard_proc.pid)
        _dashboard_proc.terminate()
        try:
            _dashboard_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _dashboard_proc.kill()
    _dashboard_proc = None


# ---------------------------------------------------------------------------
# Plánovač
# ---------------------------------------------------------------------------

def should_run_daily_buy(now_mono: float) -> bool:
    """True pokud uběhla hodina od posledního nákupního běhu."""
    return (now_mono - _last_daily_buy) >= DAILY_BUY_INTERVAL_SEC


def mark_daily_buy_done() -> None:
    global _last_daily_buy
    _last_daily_buy = time.monotonic()


def mark_forecast_recheck_done() -> None:
    global _last_forecast_recheck
    _last_forecast_recheck = time.monotonic()


def should_run_forecast_recheck(now_mono: float) -> bool:
    """True pokud uběhlo ≥ 3 hodiny od posledního rechecku."""
    return (now_mono - _last_forecast_recheck) >= FORECAST_RECHECK_INTERVAL_SEC


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

def _handle_signal(signum, frame):
    global _running
    log.info("Přijat signál %d, ukončuji…", signum)
    _running = False


signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Hlavní smyčka
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Weather Bot — standalone runner")
    parser.add_argument("--no-dashboard", action="store_true", help="Nespouštět Streamlit dashboard")
    parser.add_argument("--port", type=int, default=DASHBOARD_PORT, help="Port dashboardu (výchozí 8501)")
    parser.add_argument("--buy-now", action="store_true", help="Hned spustit daily_buy (pro test)")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Polymarket Weather Bot — Runner")
    log.info("  Monitor:    každé %d s", MONITOR_INTERVAL_SEC)
    log.info("  Fcst recheck: každé %d h", FORECAST_RECHECK_INTERVAL_SEC // 3600)
    log.info("  Daily buy:  každou hodinu (okno dle BUY_HOURS_BEFORE)")
    log.info("  Dashboard:  %s", f"http://localhost:{args.port}" if not args.no_dashboard else "vypnuto")
    log.info("  Ukončení:   Ctrl+C")
    log.info("=" * 60)

    # Případný okamžitý buy
    if args.buy_now:
        run_script("daily_buy.py", "daily_buy [--buy-now]")
        mark_daily_buy_done()

    # Spusť dashboard
    if not args.no_dashboard:
        start_dashboard(args.port)
        time.sleep(2)  # dej streamlitu chvíli na start

    # První monitor hned po startu
    run_script("monitor_positions.py", "monitor [initial]")
    last_monitor = time.monotonic()

    # -----------------------------------------------------------------------
    # Hlavní smyčka
    # -----------------------------------------------------------------------
    while _running:
        now_mono = time.monotonic()

        # --- Monitor (každé 2 minuty) ---
        if now_mono - last_monitor >= MONITOR_INTERVAL_SEC:
            run_script("monitor_positions.py", "monitor")
            last_monitor = time.monotonic()

        # --- Forecast recheck (každé 3 hodiny) ---
        if should_run_forecast_recheck(now_mono):
            run_script("forecast_recheck.py", "forecast_recheck")
            mark_forecast_recheck_done()

        # --- Daily buy (18:00 UTC) ---
        if should_run_daily_buy(now_mono):
            run_script("daily_buy.py", "daily_buy")
            mark_daily_buy_done()

        # --- Watchdog dashboardu ---
        if not args.no_dashboard:
            ensure_dashboard(args.port)

        # Spí po 10 s (dostatečně krátké aby nezmeškalo 18:00)
        for _ in range(10):
            if not _running:
                break
            time.sleep(1)

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------
    log.info("Ukončuji runner…")
    stop_dashboard()
    log.info("Hotovo.")


if __name__ == "__main__":
    main()