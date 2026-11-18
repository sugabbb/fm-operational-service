"""Background rebuild of the Lele biomass read model (consumer of ``lele_biomass_jobs``).

Every change that affects a group's biomass page (sampling create/update, group create/edit/close
including its initial weight, mortality in daily records, population adjustments) inserts a job in
the same transaction as the change. This module rebuilds the queued groups with
``rebuild_snapshot``, one group per transaction, and deletes their jobs, so the API request that
made the change never waits for the computation.

It is idempotent and safe to run from several places at once, like the main cash account events:

- after every such API response, through FastAPI ``BackgroundTasks`` (:func:`drain_in_background`);
- ``python -m app.scripts.lele_biomass_worker`` for a dedicated process or cron;
- ``python -m app.scripts.backfill_lele_sampling_results``, which rebuilds every group.

Concurrency: a per-group advisory lock serialises rebuilds of the same group, and only the jobs
locked at the start are deleted. A change committed while a group is being rebuilt therefore
keeps its own job and triggers another rebuild, so the read model always converges on the latest
data.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.db.models import LeleBiomassJob
from app.db.session import SessionLocal
from app.features.lele_biomass_queue import has_pending
from app.features.lele_biomass_service import rebuild_snapshot

logger = logging.getLogger(__name__)

# pg_advisory_xact_lock(namespace, key): keeps these locks apart from the farm write lock.
LOCK_NAMESPACE = 20261124
INT4_MAX = 2_147_483_647


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
    else:
        with SessionLocal() as owned:
            yield owned


def lock_group(db: Session, group_id: int) -> None:
    # Two-key form takes int4; a collision between two groups only serialises them, never breaks.
    db.execute(
        text("select pg_advisory_xact_lock(:namespace, :key)"),
        {"namespace": LOCK_NAMESPACE, "key": group_id % INT4_MAX},
    )


class GroupRebuildError(Exception):
    def __init__(self, group_id: int) -> None:
        super().__init__(f"rebuilding the biomass of Lele group {group_id} failed")
        self.group_id = group_id


def process_next(db: Session, skip: set[int] | None = None) -> int | None:
    """Rebuilds the group of the oldest queued job (not in ``skip``) in one transaction and
    returns its id, or None when nothing is left. Raises GroupRebuildError (after rolling back)
    when the rebuild fails."""
    stmt = select(LeleBiomassJob.group_id).order_by(LeleBiomassJob.id).limit(1).with_for_update(skip_locked=True)
    if skip:
        stmt = stmt.where(LeleBiomassJob.group_id.not_in(skip))
    group_id = db.scalar(stmt)
    if group_id is None:
        db.commit()
        return None
    try:
        lock_group(db, group_id)
        # Locked after the group lock: jobs committed from now on are not deleted by this run.
        job_ids = db.scalars(
            select(LeleBiomassJob.id).where(LeleBiomassJob.group_id == group_id).with_for_update(skip_locked=True)
        ).all()
        rebuild_snapshot(db, group_id)
        db.execute(delete(LeleBiomassJob).where(LeleBiomassJob.id.in_(job_ids)))
        db.commit()
    except Exception as exc:
        db.rollback()
        raise GroupRebuildError(group_id) from exc
    return group_id


def drain(*, session: Session | None = None, max_groups: int | None = None) -> int:
    """Rebuilds queued groups until the queue is empty (or ``max_groups`` were rebuilt). A group
    whose rebuild fails is logged and left queued for the next trigger; the others still run."""
    rebuilt = 0
    failed: set[int] = set()
    with _session(session) as db:
        if not has_pending(db):
            db.rollback()
            return 0
        while max_groups is None or rebuilt < max_groups:
            try:
                group_id = process_next(db, failed)
            except GroupRebuildError as error:
                logger.exception("%s; it stays queued", error)
                failed.add(error.group_id)
                continue
            if group_id is None:
                break
            rebuilt += 1
    return rebuilt


def drain_in_background() -> None:
    """``BackgroundTasks`` entry point. Never raises: on failure the jobs simply stay queued and are
    processed by the next trigger (another request, the worker command, or the backfill)."""
    try:
        rebuilt = drain()
        if rebuilt:
            logger.info("rebuilt the biomass of %s Lele groups", rebuilt)
    except Exception:
        logger.exception("rebuilding Lele biomass failed; the jobs stay queued for the next run")
