"""Run the leaderboard at local midnight and noon while the bot is running."""
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("huddles")


def next_run(now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    noon = midnight.replace(hour=12)
    return noon if now < noon else midnight + timedelta(days=1)


def leaderboard_worker(stop):
    zone = ZoneInfo(os.getenv("LEADERBOARD_TIMEZONE", "America/Dominica"))
    while not stop.is_set():
        target = next_run(datetime.now(zone))
        log.info("Next leaderboard: %s", target.isoformat())
        while not stop.is_set():
            remaining = (target - datetime.now(zone)).total_seconds()
            if remaining <= 0:
                break
            stop.wait(min(60, remaining))
        if stop.is_set():
            return
        try:
            subprocess.run([sys.executable, str(Path(__file__).with_name("leaderboard"))],
                           check=True, timeout=300)
            log.info("Scheduled leaderboard sent")
        except Exception:
            log.exception("Scheduled leaderboard failed")
