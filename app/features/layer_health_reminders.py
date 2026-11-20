"""Layer health reminders: the nightly rebuild and the stored reminders the APIs read.

The reminders are not computed when the ABK Web App asks for them. A nightly job
(``python -m app.scripts.layer_health_reminder_job``) computes them with
``layer_health_schedule`` and stores them in ``layer_health_daily_reminders`` (one row per active
Ayam group and date, with its age) and ``layer_health_daily_reminder_tasks`` (the vaccinations and
medicines of that group and date, program details copied). The APIs only read those rows.

Each run replaces the dates from its run date to ``REMINDER_DAYS`` days later, so every day the
coming week is refreshed with the latest program, group ages, and groups (a group closed since the
previous run loses its coming reminders; a new group gets them). Earlier dates are kept as they were
last computed. If a run is missed, the rows written by the previous runs still cover the coming
days, with the program as it was then.

Snoozing: ``snooze`` stops reminding one occurrence (``occurrence_key``: a vaccination's whole age
week, or one medicine course day). It is kept in ``layer_health_reminder_snoozes``, apart from the
rows the job rewrites, so it also holds for the tasks of later runs. This module does not know what
snoozes an occurrence; the ABK recording an activity (``layer_health_activities``) is one reason.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    LayerHealthDailyReminder,
    LayerHealthDailyReminderTask,
    LayerHealthReminderSnooze,
    LayerMedicineProgram,
    LayerVaccination,
    LivestockGroup,
)
from app.features.common import now_utc, today_local
from app.features.layer_health_schedule import Task, dates, group_day

logger = logging.getLogger(__name__)

# The job covers its run date and the next REMINDER_DAYS days; the ABK can look that far ahead.
REMINDER_DAYS = 7
# pg_advisory_xact_lock key of the rebuild: two runs never interleave.
REBUILD_LOCK = 20261125

# Youngest age first, the same order in the program lists and in the reminders.
VACCINATION_ORDER = (LayerVaccination.age_week, func.lower(LayerVaccination.vaccine_name), LayerVaccination.id)
MEDICINE_ORDER = (
    LayerMedicineProgram.start_age_week,
    LayerMedicineProgram.start_day,
    func.lower(LayerMedicineProgram.medicine_name),
    LayerMedicineProgram.id,
)


@dataclass(frozen=True)
class RebuildResult:
    start: date
    end: date
    groups: int
    reminders: int
    tasks: int


def task_row(task: Task, position: int) -> LayerHealthDailyReminderTask:
    row = LayerHealthDailyReminderTask(position=position, task_key=task.key, occurrence_key=task.occurrence_key)
    if task.vaccination is not None:
        vaccination = task.vaccination
        row.type, row.vaccination_id = "VACCINATION", vaccination.id
        row.name, row.target_disease = vaccination.vaccine_name, vaccination.target_disease
        row.method, row.dosage, row.notes = vaccination.method, vaccination.dosage, vaccination.notes
        row.window_start, row.window_end = task.window
    else:
        medicine = task.medicine
        assert medicine is not None
        row.type, row.medicine_id = "MEDICINE", medicine.id
        row.name, row.purpose = medicine.medicine_name, medicine.purpose
        row.method, row.dosage, row.notes = medicine.method, medicine.dosage, medicine.notes
        row.course_day, row.course_days = task.course_day, medicine.course_days
    return row


def rebuild(db: Session, today: date | None = None) -> RebuildResult:
    """Replaces the reminders from ``today`` (default: today in APP_TIMEZONE) to REMINDER_DAYS days
    later with the current active program and active Ayam groups, in one transaction (readers see
    either the previous or the new reminders, never a mix), and commits."""
    start = today or today_local()
    end = start + timedelta(days=REMINDER_DAYS)
    db.execute(text("select pg_advisory_xact_lock(:key)"), {"key": REBUILD_LOCK})
    groups = db.scalars(
        select(LivestockGroup)
        .where(LivestockGroup.species == "AYAM", LivestockGroup.status == "ACTIVE")
        .order_by(LivestockGroup.id)
    ).all()
    vaccinations = db.scalars(
        select(LayerVaccination).where(LayerVaccination.is_active.is_(True)).order_by(*VACCINATION_ORDER)
    ).all()
    medicines = db.scalars(
        select(LayerMedicineProgram).where(LayerMedicineProgram.is_active.is_(True)).order_by(*MEDICINE_ORDER)
    ).all()
    # Every group, also the ones closed since the previous run; the tasks go with them (ON DELETE CASCADE).
    db.execute(delete(LayerHealthDailyReminder).where(LayerHealthDailyReminder.reminder_date.between(start, end)))
    computed_at = now_utc()
    reminders = tasks = 0
    for on in dates(start, end):
        for group in groups:
            day = group_day(on, group, vaccinations, medicines)
            if day is None:  # before the group's start date
                continue
            db.add(
                LayerHealthDailyReminder(
                    group_id=group.id,
                    reminder_date=on,
                    age_week=day.age.week,
                    age_day=day.age.day,
                    computed_at=computed_at,
                    tasks=[task_row(task, position) for position, task in enumerate(day.tasks, start=1)],
                )
            )
            reminders += 1
            tasks += len(day.tasks)
    db.commit()
    result = RebuildResult(start, end, len(groups), reminders, tasks)
    logger.info(
        "layer health reminders %s..%s rebuilt: %s groups, %s group-days, %s tasks",
        start,
        end,
        result.groups,
        reminders,
        tasks,
    )
    return result


# --- Snoozing --------------------------------------------------------------------------------


def find_task(db: Session, task_key: str, *, for_update: bool = False) -> LayerHealthDailyReminderTask | None:
    """A stored reminder task, with its reminder (group and date) loaded."""
    stmt = (
        select(LayerHealthDailyReminderTask)
        .where(LayerHealthDailyReminderTask.task_key == task_key)
        .options(selectinload(LayerHealthDailyReminderTask.reminder).selectinload(LayerHealthDailyReminder.group))
    )
    if for_update:
        stmt = stmt.with_for_update(of=LayerHealthDailyReminderTask)
    return db.scalar(stmt)


def snoozed_at(db: Session, occurrence_key: str) -> datetime | None:
    return db.scalar(
        select(LayerHealthReminderSnooze.snoozed_at).where(LayerHealthReminderSnooze.occurrence_key == occurrence_key)
    )


def snooze(
    db: Session, occurrence_key: str, *, source: str, source_id: int | None, employee_id: int | None
) -> LayerHealthReminderSnooze:
    """Stops reminding every task of an occurrence (flushed, not committed: part of the caller's
    transaction). An occurrence is snoozed once; a second snooze violates the unique key."""
    row = LayerHealthReminderSnooze(
        occurrence_key=occurrence_key,
        snoozed_at=now_utc(),
        snoozed_by_employee_id=employee_id,
        source=source,
        source_id=source_id,
    )
    db.add(row)
    db.flush()
    return row


# --- Reading ---------------------------------------------------------------------------------


def task_out(row: LayerHealthDailyReminderTask, on: date, snoozed: datetime | None) -> dict[str, object]:
    vaccination = row.type == "VACCINATION"
    return {
        "key": row.task_key,
        "type": row.type,
        "source_id": row.vaccination_id if vaccination else row.medicine_id,
        "name": row.name,
        "target_disease": row.target_disease,
        "purpose": row.purpose,
        "method": row.method,
        "dosage": row.dosage,
        "notes": row.notes,
        # Vaccination: give it once, on any day from..to (its age week); this is day n of 7.
        "window": (
            {
                "from": row.window_start.isoformat(),
                "to": row.window_end.isoformat(),
                "day": (on - row.window_start).days + 1,
                "days": 7,
            }
            if vaccination and row.window_start and row.window_end
            else None
        ),
        # Medicine: day n of a course of consecutive days.
        "course": None if vaccination else {"day": row.course_day, "days": row.course_days},
        # Set when the occurrence is no longer reminded (only listed with include_snoozed).
        "snoozed_at": snoozed.isoformat() if snoozed else None,
    }


def reminder_out(
    row: LayerHealthDailyReminder, snoozes: dict[str, datetime], include_snoozed: bool
) -> dict[str, object]:
    group = row.group
    return {
        "group": {"id": group.id, "code": group.code, "name": group.name, "location": group.location},
        "age": {"week": row.age_week, "day": row.age_day},
        "computed_at": row.computed_at.isoformat(),
        "tasks": [
            task_out(task, row.reminder_date, snoozes.get(task.occurrence_key))
            for task in row.tasks
            if include_snoozed or task.occurrence_key not in snoozes
        ],
    }


def stored_reminders(
    db: Session,
    start: date,
    end: date,
    *,
    group_id: int | None = None,
    active_groups_only: bool = False,
    include_snoozed: bool = False,
) -> dict[str, object]:
    """The stored reminders from start to end (both included): every date is listed, each with the
    groups stored for it (by group name), also a group without any task. Snoozed tasks are left out
    unless ``include_snoozed``."""
    stmt = (
        select(LayerHealthDailyReminder)
        .join(LayerHealthDailyReminder.group)
        .where(LayerHealthDailyReminder.reminder_date.between(start, end))
        .options(selectinload(LayerHealthDailyReminder.group), selectinload(LayerHealthDailyReminder.tasks))
        .order_by(LayerHealthDailyReminder.reminder_date, LivestockGroup.name, LivestockGroup.id)
    )
    if group_id is not None:
        stmt = stmt.where(LayerHealthDailyReminder.group_id == group_id)
    if active_groups_only:
        # A group closed during the day drops out at once, before the next rebuild.
        stmt = stmt.where(LivestockGroup.status == "ACTIVE")
    by_date: dict[date, list[LayerHealthDailyReminder]] = {}
    rows = db.scalars(stmt).all()
    for row in rows:
        by_date.setdefault(row.reminder_date, []).append(row)
    occurrence_keys = {task.occurrence_key for row in rows for task in row.tasks}
    snoozes: dict[str, datetime] = (
        dict(
            db.execute(
                select(LayerHealthReminderSnooze.occurrence_key, LayerHealthReminderSnooze.snoozed_at).where(
                    LayerHealthReminderSnooze.occurrence_key.in_(occurrence_keys)
                )
            )
            .tuples()
            .all()
        )
        if occurrence_keys
        else {}
    )
    today = today_local()
    return {
        "today": today.isoformat(),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": [
            {
                "date": on.isoformat(),
                "days_from_today": (on - today).days,
                "groups": [reminder_out(row, snoozes, include_snoozed) for row in by_date.get(on, [])],
            }
            for on in dates(start, end)
        ],
    }


def active_ayam_group(db: Session, group_id: int) -> LivestockGroup | None:
    return db.scalar(
        select(LivestockGroup).where(
            LivestockGroup.id == group_id, LivestockGroup.species == "AYAM", LivestockGroup.status == "ACTIVE"
        )
    )
