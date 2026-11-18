"""Populate ``lele_sampling_results`` and the Lele biomass read model from the source tables.

This is a manual data migration, independent of the Alembic schema migrations. With the same code
the API uses:

1. for every Lele group that has samplings it recomputes the per-fish average weight and the real
   ADG of each sampling (``refresh_group_results``);
2. for every Lele group it rebuilds the biomass read model read by ``GET /lele-biomass/{id}``
   (``lele_biomass_snapshots`` and ``lele_biomass_chart_*``, via ``rebuild_snapshot``).

It is idempotent, so it can be re-run at any time, e.g. to repair the data after samplings or
groups were edited directly in the database, or after changing ``LELE_TARGET_WEIGHT_G`` /
``LELE_DEFAULT_ADG_G``.

It takes the same transactional advisory lock as sampling writes, so it is safe while the API is
running.

Usage (from the backend directory, with DATABASE_URL set or present in .env)::

    python -m app.scripts.backfill_lele_sampling_results --dry-run   # show what would change
    python -m app.scripts.backfill_lele_sampling_results             # write the results
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.db.models import LeleSampling, LeleSamplingMeasurement, LeleSamplingResult, LivestockGroup
from app.db.session import SessionLocal
from app.features.farm_service import lock_balances
from app.features.lele_biomass_jobs import lock_group
from app.features.lele_biomass_service import rebuild_snapshot, refresh_group_results

COMPARED_COLUMNS = (
    "fish_count",
    "total_weight_kg",
    "average_weight_g",
    "previous_sampling_id",
    "days_since_previous",
    "weight_gain_g",
    "adg_g",
)


@dataclass(frozen=True)
class BackfillResult:
    groups: int
    samplings: int
    created: int
    updated: int
    removed: int
    snapshots: int
    dry_run: bool

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.removed)


def snapshot(db: Session) -> dict[int, tuple[object, ...]]:
    return {
        row.sampling_id: tuple(getattr(row, column) for column in COMPARED_COLUMNS)
        for row in db.scalars(select(LeleSamplingResult))
    }


def backfill(db: Session, *, dry_run: bool = False) -> BackfillResult:
    """Recomputes every result. Commits unless ``dry_run`` (then rolls back)."""
    try:
        lock_balances(db)
        before = snapshot(db)
        group_ids = db.scalars(select(LeleSampling.group_id).distinct().order_by(LeleSampling.group_id)).all()
        samplings = 0
        for group_id in group_ids:
            samplings += len(refresh_group_results(db, group_id))
        # A result whose sampling has no measurements cannot be computed. The API never creates
        # such a sampling, so only a manual database edit can leave one behind.
        orphaned = db.scalars(
            select(LeleSamplingResult).where(
                ~exists().where(LeleSamplingMeasurement.sampling_id == LeleSamplingResult.sampling_id)
            )
        ).all()
        for row in orphaned:
            db.delete(row)
        db.flush()
        after = snapshot(db)
        lele_groups = db.scalars(
            select(LivestockGroup.id).where(LivestockGroup.species == "LELE").order_by(LivestockGroup.id)
        ).all()
        for group_id in lele_groups:
            lock_group(db, group_id)
            rebuild_snapshot(db, group_id)
        result = BackfillResult(
            groups=len(group_ids),
            samplings=samplings,
            created=len(after.keys() - before.keys()),
            updated=sum(1 for key in after.keys() & before.keys() if after[key] != before[key]),
            removed=len(before.keys() - after.keys()),
            snapshots=len(lele_groups),
            dry_run=dry_run,
        )
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def print_result(result: BackfillResult) -> None:
    rows = [
        ("Lele groups with samplings:", result.groups),
        ("Samplings computed:", result.samplings),
        ("Results created:", result.created),
        ("Results updated:", result.updated),
        ("Results removed:", result.removed),
        ("Biomass snapshots rebuilt:", result.snapshots),
    ]
    width = max(len(label) for label, _ in rows) + 1
    for label, value in rows:
        print(f"{label:<{width}}{value}")
    if result.dry_run:
        print("Dry run: nothing was written.")
    elif result.changed:
        print("lele_sampling_results updated; biomass snapshots rebuilt.")
    else:
        print("lele_sampling_results already up to date; biomass snapshots rebuilt.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.backfill_lele_sampling_results",
        description="Recompute every Lele sampling's weight and real ADG, and every Lele group's biomass read model.",
    )
    parser.add_argument("--dry-run", action="store_true", help="calculate and print without writing anything")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        print_result(backfill(db, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
