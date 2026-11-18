"""Queue a Lele group for a background rebuild of its biomass read model.

Kept free of business imports so any write path can call it. The rebuild itself lives in
``app.features.lele_biomass_jobs``.
"""

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.models import LeleBiomassJob, LivestockGroup


def enqueue(db: Session, group: LivestockGroup, reason: str) -> None:
    """Queues ``group`` in the caller's transaction (committed atomically with the change). Only
    Lele groups have a biomass read model; other species are ignored."""
    if group.species == "LELE":
        db.add(LeleBiomassJob(group_id=group.id, reason=reason))


def enqueue_id(db: Session, group_id: int, reason: str) -> None:
    """Same as :func:`enqueue` for a group known to be Lele (e.g. a sampling's group)."""
    db.add(LeleBiomassJob(group_id=group_id, reason=reason))


def has_pending(db: Session, group_id: int | None = None) -> bool:
    """Whether a rebuild is still queued (for one group, or any)."""
    condition = LeleBiomassJob.group_id == group_id if group_id is not None else LeleBiomassJob.id.is_not(None)
    return bool(db.scalar(select(exists().where(condition))))
