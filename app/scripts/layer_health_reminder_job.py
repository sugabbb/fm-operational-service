"""Nightly job: prepare the layer health reminders read by the ABK Web App.

Computes the vaccinations and medicines due for every active Ayam group from today to 7 days later
(today in APP_TIMEZONE) with the current program, and replaces those dates in
``layer_health_daily_reminders``. Earlier dates are left as they were.

Run it every night just after midnight (APP_TIMEZONE), so that "today" is the new day, and once
after deploying. Run it by hand after changing the program or a group when the change must show
before the next night. It is idempotent and safe while the API is running: two runs never
interleave, and readers see either the previous or the new reminders.

    python -m app.scripts.layer_health_reminder_job                    # today and the next 7 days
    python -m app.scripts.layer_health_reminder_job --date 2026-03-02  # as if today were that date

Example crontab line (server clock in Asia/Jakarta):

    5 0 * * *  cd /srv/operational-service && .venv/bin/python -m app.scripts.layer_health_reminder_job
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import date

from app.core.config import settings
from app.db.session import SessionLocal
from app.features.layer_health_reminders import rebuild

logger = logging.getLogger("app.scripts.layer_health_reminder_job")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.layer_health_reminder_job",
        description="Rebuild the layer health reminders of today and the next 7 days.",
    )
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="first date (YYYY-MM-DD); default: today")
    args = parser.parse_args(argv)
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        with SessionLocal() as db:
            result = rebuild(db, args.date)
    except Exception:
        logger.exception("rebuilding the layer health reminders failed")
        return 1
    print(
        f"Layer health reminders {result.start}..{result.end}: {result.groups} active groups, "
        f"{result.reminders} group-days, {result.tasks} tasks."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
