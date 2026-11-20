"""Layer health activities: what the ABK did with a reminder task (done, or skipped with a reason).

This is the activity context, standalone from the reminder context (``layer_health_reminders``):

- recording copies every detail of the reminder task (group, date, age, program details,
  vaccination week or course day) into ``layer_health_daily_activities``; nothing refers to the
  reminder rows, which the nightly job rewrites;
- in the same transaction it asks the reminder context to snooze the task's occurrence, so the
  vaccination stops being reminded for the rest of its week, or the medicine for that course day.

The dependency goes one way: activities use the reminder context, never the reverse, so reminders
can later serve other contexts than the health program.

An occurrence is recorded once (``409 LAYER_HEALTH_ACTIVITY_EXISTS`` afterwards). Activities are
not edited.
"""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status
from pydantic import Field, StringConstraints, model_validator
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import selectinload

from app.core.dependencies import ActiveUser, CurrentAbk, DbSession
from app.core.errors import conflict, not_found, validation
from app.core.pagination import PageDep, StrictModel
from app.db.models import AbkAccount, LayerHealthDailyActivity, LayerHealthDailyReminderTask
from app.features.abk import require_scope
from app.features.common import audit, now_utc, today_local
from app.features.layer_health_reminders import find_task, snooze, snoozed_at

router = APIRouter(tags=["Layer Health Program"])
abk_router = APIRouter(prefix="/abk", tags=["ABK Web App"])

# pg_advisory_xact_lock(namespace, hashtext(occurrence_key)): one recording per occurrence at a time.
OCCURRENCE_LOCK_NAMESPACE = 20261126
SNOOZE_SOURCE = "LAYER_HEALTH_ACTIVITY"

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=3000)] | None


class ActivityInput(StrictModel):
    task_key: str = Field(min_length=1, max_length=120, description="The reminder task's key.")
    status: Literal["DONE", "SKIPPED"]
    skip_reason: Text = Field(default=None, description="Required when SKIPPED (why it was not done); not for DONE.")
    notes: Text = None

    @model_validator(mode="after")
    def reason(self) -> ActivityInput:
        self.skip_reason = self.skip_reason or None
        self.notes = self.notes or None
        if self.status == "SKIPPED" and not self.skip_reason:
            raise ValueError("skip_reason is required when the task is skipped")
        if self.status == "DONE" and self.skip_reason:
            raise ValueError("skip_reason is only for a skipped task")
        return self


def activity_out(row: LayerHealthDailyActivity, *, staff: bool = False) -> dict[str, object]:
    group = row.group
    vaccination = row.type == "VACCINATION"
    data: dict[str, object] = {
        "id": row.id,
        "status": row.status,
        "skip_reason": row.skip_reason,
        "notes": row.notes,
        "activity_date": row.activity_date.isoformat(),
        "recorded_at": row.recorded_at.isoformat(),
        "group": {"id": group.id, "code": group.code, "name": group.name, "location": group.location},
        "age": {"week": row.age_week, "day": row.age_day},
        "type": row.type,
        "source_id": row.vaccination_id if vaccination else row.medicine_id,
        "name": row.name,
        "target_disease": row.target_disease,
        "purpose": row.purpose,
        "method": row.method,
        "dosage": row.dosage,
        "program_notes": row.program_notes,
        "window": (
            {
                "from": row.window_start.isoformat(),
                "to": row.window_end.isoformat(),
                "day": (row.activity_date - row.window_start).days + 1,
                "days": 7,
            }
            if vaccination and row.window_start and row.window_end
            else None
        ),
        "course": None if vaccination else {"day": row.course_day, "days": row.course_days},
        "reminder_task_key": row.reminder_task_key,
    }
    if staff:
        data["occurrence_key"] = row.occurrence_key
        data["recorded_by"] = {"employee_id": row.recorded_by.id, "name": row.recorded_by.name}
    return data


def activity_query() -> Select[tuple[LayerHealthDailyActivity]]:
    return select(LayerHealthDailyActivity).options(
        selectinload(LayerHealthDailyActivity.group), selectinload(LayerHealthDailyActivity.recorded_by)
    )


def copy_task(task: LayerHealthDailyReminderTask) -> LayerHealthDailyActivity:
    reminder = task.reminder
    return LayerHealthDailyActivity(
        group_id=reminder.group_id,
        activity_date=reminder.reminder_date,
        age_week=reminder.age_week,
        age_day=reminder.age_day,
        type=task.type,
        vaccination_id=task.vaccination_id,
        medicine_id=task.medicine_id,
        name=task.name,
        target_disease=task.target_disease,
        purpose=task.purpose,
        method=task.method,
        dosage=task.dosage,
        program_notes=task.notes,
        window_start=task.window_start,
        window_end=task.window_end,
        course_day=task.course_day,
        course_days=task.course_days,
        reminder_task_key=task.task_key,
        occurrence_key=task.occurrence_key,
    )


def filtered(
    stmt: Select[tuple[LayerHealthDailyActivity]],
    from_date: date | None,
    to_date: date | None,
    activity_status: str | None,
    activity_type: str | None,
    group_id: int | None,
) -> Select[tuple[LayerHealthDailyActivity]]:
    if from_date:
        stmt = stmt.where(LayerHealthDailyActivity.activity_date >= from_date)
    if to_date:
        stmt = stmt.where(LayerHealthDailyActivity.activity_date <= to_date)
    if activity_status:
        stmt = stmt.where(LayerHealthDailyActivity.status == activity_status)
    if activity_type:
        stmt = stmt.where(LayerHealthDailyActivity.type == activity_type)
    if group_id:
        stmt = stmt.where(LayerHealthDailyActivity.group_id == group_id)
    return stmt


def load(db: DbSession, stmt: Select[tuple[LayerHealthDailyActivity]], activity_id: int) -> LayerHealthDailyActivity:
    row = db.scalar(stmt.where(LayerHealthDailyActivity.id == activity_id).execution_options(populate_existing=True))
    if not row:
        raise not_found("Layer health activity")
    return row


def page_of(
    db: DbSession, stmt: Select[tuple[LayerHealthDailyActivity]], page: PageDep, staff: bool
) -> dict[str, object]:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = db.scalars(
        stmt.order_by(LayerHealthDailyActivity.activity_date.desc(), LayerHealthDailyActivity.id.desc())
        .offset(page.offset)
        .limit(page.per_page)
    ).all()
    return {"data": [activity_out(row, staff=staff) for row in rows], "meta": page.meta(total)}


StatusQuery = Annotated[str | None, Query(alias="status", pattern="^(DONE|SKIPPED)$")]
TypeQuery = Annotated[str | None, Query(alias="type", pattern="^(VACCINATION|MEDICINE)$")]
FromQuery = Annotated[date | None, Query(alias="from")]
ToQuery = Annotated[date | None, Query(alias="to")]


# --- ABK -----------------------------------------------------------------------------------------


def own_activities(account: AbkAccount) -> Select[tuple[LayerHealthDailyActivity]]:
    return activity_query().where(LayerHealthDailyActivity.recorded_by_employee_id == account.employee_id)


@abk_router.post("/layer-health-activities", status_code=status.HTTP_201_CREATED)
def record_activity(payload: ActivityInput, account: CurrentAbk, db: DbSession) -> dict[str, object]:
    """Records a reminder task as DONE, or SKIPPED with ``skip_reason``, for a task dated today or
    earlier. Its details are copied into the activity, and the task's occurrence stops being
    reminded: a vaccination for the rest of its age week, a medicine for that course day."""
    require_scope(account, "AYAM", "Vaccination and medicine activities are only for ABK assigned to chicken (Ayam).")
    task = find_task(db, payload.task_key, for_update=True)  # the nightly job waits for this recording
    if task is None:
        raise not_found("Layer health reminder task")
    reminder = task.reminder
    if reminder.group.status != "ACTIVE":
        raise conflict("LAYER_HEALTH_GROUP_NOT_ACTIVE", "The group of this task is no longer active.")
    if reminder.reminder_date > today_local():
        raise validation("task_key", "A task of a later date cannot be recorded yet.")
    db.execute(
        text("select pg_advisory_xact_lock(:namespace, hashtext(:key))"),
        {"namespace": OCCURRENCE_LOCK_NAMESPACE, "key": task.occurrence_key},
    )
    existing = db.scalar(
        select(LayerHealthDailyActivity).where(LayerHealthDailyActivity.occurrence_key == task.occurrence_key)
    )
    if existing:
        raise conflict(
            "LAYER_HEALTH_ACTIVITY_EXISTS",
            "This task was already recorded.",
            status=existing.status,
            activity_date=existing.activity_date.isoformat(),
        )
    if snoozed_at(db, task.occurrence_key):
        raise conflict("LAYER_HEALTH_REMINDER_SNOOZED", "This task is no longer reminded.")
    row = copy_task(task)
    row.status, row.skip_reason, row.notes = payload.status, payload.skip_reason, payload.notes
    row.recorded_by_employee_id, row.recorded_at = account.employee_id, now_utc()
    db.add(row)
    db.flush()
    snooze(db, task.occurrence_key, source=SNOOZE_SOURCE, source_id=row.id, employee_id=account.employee_id)
    audit(db, row, "layer_health_activity_recorded", None, reason=f"abk:{account.username}")
    db.commit()
    return {"data": activity_out(load(db, own_activities(account), row.id))}


@abk_router.get("/layer-health-activities")
def my_activities(
    account: CurrentAbk,
    db: DbSession,
    page: PageDep,
    from_date: FromQuery = None,
    to_date: ToQuery = None,
    activity_status: StatusQuery = None,
    activity_type: TypeQuery = None,
    group_id: int | None = None,
) -> dict[str, object]:
    """The activities this ABK recorded (never anyone else's), latest date first."""
    stmt = filtered(own_activities(account), from_date, to_date, activity_status, activity_type, group_id)
    return page_of(db, stmt, page, staff=False)


@abk_router.get("/layer-health-activities/{activity_id}")
def my_activity(activity_id: int, account: CurrentAbk, db: DbSession) -> dict[str, object]:
    """One activity this ABK recorded; another ABK's activity answers 404 like a missing one."""
    return {"data": activity_out(load(db, own_activities(account), activity_id))}


# --- Staff ---------------------------------------------------------------------------------------


@router.get("/layer-health-activities")
def list_activities(
    _: ActiveUser,
    db: DbSession,
    page: PageDep,
    from_date: FromQuery = None,
    to_date: ToQuery = None,
    activity_status: StatusQuery = None,
    activity_type: TypeQuery = None,
    group_id: int | None = None,
    employee_id: int | None = None,
) -> dict[str, object]:
    """Every recorded activity, latest date first (``status=SKIPPED`` lists the skipped ones with
    their reasons)."""
    stmt = filtered(activity_query(), from_date, to_date, activity_status, activity_type, group_id)
    if employee_id:
        stmt = stmt.where(LayerHealthDailyActivity.recorded_by_employee_id == employee_id)
    return page_of(db, stmt, page, staff=True)


@router.get("/layer-health-activities/{activity_id}")
def get_activity(activity_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    return {"data": activity_out(load(db, activity_query(), activity_id), staff=True)}
