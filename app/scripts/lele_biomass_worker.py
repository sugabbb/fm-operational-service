"""Rebuilds the Lele biomass read model of queued groups (``lele_biomass_jobs``).

The API already rebuilds them right after each response that changes a group (FastAPI
background task). Run this when you want a dedicated consumer, for example on hosts where work
after the response is not reliable (CPU throttled or instances shut down once the response is
sent), or from cron as a safety net:

    python -m app.scripts.lele_biomass_worker            # keep running, poll every 5 s
    python -m app.scripts.lele_biomass_worker --once     # drain the queue and exit (cron)

Several consumers can run at once; rebuilds of the same group are serialised.
"""

import argparse
import logging
import signal
import sys
import time
from collections.abc import Sequence
from types import FrameType

from app.core.config import settings
from app.features.lele_biomass_jobs import drain

logger = logging.getLogger("app.scripts.lele_biomass_worker")


class Stop:
    requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        logger.info("signal %s received; stopping after the current group", signum)
        self.requested = True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.lele_biomass_worker",
        description="Rebuild the biomass read model of the Lele groups queued in lele_biomass_jobs.",
    )
    parser.add_argument("--once", action="store_true", help="drain the queue once and exit")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between polls (default 5)")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be > 0")

    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = Stop()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while True:
        try:
            rebuilt = drain()
            if rebuilt:
                logger.info("rebuilt the biomass of %s Lele groups", rebuilt)
        except Exception:
            logger.exception("rebuilding Lele biomass failed; retrying")
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
