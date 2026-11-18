"""Delivers approved ABK egg sales to finance-service (``abk_egg_sales`` with PENDING status).

The API already tries right after each approval or resend (FastAPI background task). Run this to
retry the attempts that failed while finance-service was unavailable; each sale waits for its
back-off (``next_attempt_at``) before the next attempt:

    python -m app.scripts.finance_egg_sale_worker            # keep running, poll every 30 s
    python -m app.scripts.finance_egg_sale_worker --once     # deliver what is due and exit (cron)

Several workers can run at once; a sale is never sent by two of them at the same time.
"""

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence
from types import FrameType

from app.core.config import settings
from app.features.abk_egg_sales import deliver_pending

logger = logging.getLogger("app.scripts.finance_egg_sale_worker")


class Stop:
    requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        logger.info("signal %s received; stopping after the current sale", signum)
        self.requested = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.finance_egg_sale_worker",
        description="Deliver the approved ABK egg sales that finance-service has not confirmed yet.",
    )
    parser.add_argument("--once", action="store_true", help="deliver the due sales once and exit")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls (default 30)")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            tried = deliver_pending(max_sales=None)
            if tried:
                logger.info("tried to deliver %s egg sales", tried)
        except Exception:
            logger.exception("delivering egg sales failed; retrying")
            if args.once:
                return 1
        if args.once:
            return 0
        deadline = time.monotonic() + args.interval
        while not stop.requested and time.monotonic() < deadline:
            time.sleep(min(0.2, args.interval))
        if stop.requested:
            return 0


if __name__ == "__main__":
    sys.exit(main())
