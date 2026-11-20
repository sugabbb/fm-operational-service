"""Layer health program (Admin/Superadmin): vaccination schedule, medicine program, and reminders.

Both lists are farm-wide: one program for every Ayam (laying hen) group, editable at any time.
Everything is timed by the hens' age (see ``layer_health_schedule``), so the same program lines up
with each group whatever age it arrived at. The daily reminders are computed from the active items
by the nightly job (``layer_health_reminders``): a change applies to the coming week from the next
run.

Items are never deleted. Deactivate one (``is_active: false``) when it no longer applies; it can be
reactivated later, and its name stays reserved in the meantime.

The vaccination schedule can also be replaced as a whole from an Excel sheet (template download and
upload; see ``layer_vaccination_excel`` for the file format).
"""

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, File, Query, Response, UploadFile, status
from pydantic import Field, StringConstraints, ValidationError, model_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.core.dependencies import ActiveUser, DbSession
from app.core.errors import AppError, conflict, not_found, validation
from app.core.pagination import StrictModel
from app.db.models import LayerMedicineProgram, LayerVaccination, LivestockGroup, User
from app.features.common import audit, model_snapshot, today_local
from app.features.layer_health_reminders import MEDICINE_ORDER, REMINDER_DAYS, VACCINATION_ORDER, stored_reminders
from app.features.layer_health_schedule import INTERVAL_DAYS
from app.features.layer_vaccination_excel import (
    COLUMNS,
    FILE_NAME,
    MAX_FILE_BYTES,
    MEDIA_TYPE,
    SheetRow,
    read_rows,
    template,
)

router = APIRouter(tags=["Layer Health Program"])

# Laying hens are usually kept well under 2 years; a larger value is almost certainly a typo
# (for example an age in days).
MAX_AGE_WEEK = 200
# Longest allowed gap between two courses of the same medicine, per unit (about one year).
MAX_INTERVAL = {"DAY": 365, "WEEK": 52, "MONTH": 12}
MAX_COURSE_DAYS = 30
# Longest period the staff reminder view covers in one request.
MAX_REMINDER_RANGE_DAYS = 92

Method = Literal[
    "EYE_DROP",
    "NASAL_DROP",
    "ORAL",
    "DRINKING_WATER",
    "FEED",
    "SPRAY",
    "INTRAMUSCULAR",
    "SUBCUTANEOUS",
    "WING_WEB",
    "OTHER",
]
IntervalUnit = Literal["DAY", "WEEK", "MONTH"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
OptionalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None
Notes = Annotated[str, StringConstraints(strip_whitespace=True, max_length=3000)] | None
AgeWeek = Annotated[int, Field(ge=0, le=MAX_AGE_WEEK, description="Age of the hens, in whole weeks.")]

METHOD_DESCRIPTION = "How it is given. Use OTHER (and explain in notes) when none fits."


def blank_to_none(value: str | None) -> str | None:
    return value or None


class VaccinationInput(StrictModel):
    age_week: AgeWeek = Field(description="Due on day 1 of this age week.")
    vaccine_name: Name
    target_disease: OptionalText = Field(default=None, description='Diseases it protects against, e.g. "ND, IB".')
    method: Method = Field(description=METHOD_DESCRIPTION)
    dosage: OptionalText = Field(default=None, description='Free text, e.g. "1 tetes/ekor" or "0,5 ml/ekor".')
    notes: Notes = None


class VaccinationUpdate(VaccinationInput):
    is_active: bool


class MedicineInput(StrictModel):
    medicine_name: Name
    purpose: OptionalText = Field(default=None, description='What it is given for, e.g. "Vitamin".')
    method: Method = Field(description=METHOD_DESCRIPTION)
    dosage: OptionalText = None
    start_age_week: AgeWeek = Field(description="Age week of the first course.")
    start_day: int = Field(
        default=1, ge=1, le=7, description="Day of start_age_week (1 = its first day) on which the first course starts."
    )
    course_days: int = Field(
        default=1, ge=1, le=MAX_COURSE_DAYS, description="Consecutive days of one course (1: a single day)."
    )
    interval_value: int = Field(ge=1, description="A new course starts every interval_value interval_unit.")
    interval_unit: IntervalUnit = Field(description="DAY, WEEK, or MONTH (4 age weeks, so always the same day).")
    notes: Notes = None

    @model_validator(mode="after")
    def interval_and_course(self) -> MedicineInput:
        limit = MAX_INTERVAL[self.interval_unit]
        if self.interval_value > limit:
            raise ValueError(f"interval_value must be at most {limit} for {self.interval_unit}")
        if self.course_days > self.interval_value * INTERVAL_DAYS[self.interval_unit]:
            raise ValueError("course_days cannot be longer than the interval between two courses")
        return self


class MedicineUpdate(MedicineInput):
    is_active: bool


def vaccination_out(row: LayerVaccination) -> dict[str, object]:
    return {
        "id": row.id,
        "age_week": row.age_week,
        "vaccine_name": row.vaccine_name,
        "target_disease": row.target_disease,
        "method": row.method,
        "dosage": row.dosage,
        "notes": row.notes,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def medicine_out(row: LayerMedicineProgram) -> dict[str, object]:
    return {
        "id": row.id,
        "medicine_name": row.medicine_name,
        "purpose": row.purpose,
        "method": row.method,
        "dosage": row.dosage,
        "start_age_week": row.start_age_week,
        "start_day": row.start_day,
        "course_days": row.course_days,
        "interval_value": row.interval_value,
        "interval_unit": row.interval_unit,
        "notes": row.notes,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def vaccination_values(payload: VaccinationInput) -> dict[str, object]:
    values = payload.model_dump(exclude={"is_active"})
    for key in ("target_disease", "dosage", "notes"):
        values[key] = blank_to_none(values[key])
    return values


def medicine_values(payload: MedicineInput) -> dict[str, object]:
    values = payload.model_dump(exclude={"is_active"})
    for key in ("purpose", "dosage", "notes"):
        values[key] = blank_to_none(values[key])
    return values


def assert_vaccination_free(db: DbSession, payload: VaccinationInput, vaccination_id: int | None = None) -> None:
    duplicate = db.scalar(
        select(LayerVaccination.id).where(
            LayerVaccination.age_week == payload.age_week,
            func.lower(LayerVaccination.vaccine_name) == payload.vaccine_name.lower(),
            LayerVaccination.id != (vaccination_id or 0),
        )
    )
    if duplicate:
        raise conflict(
            "LAYER_VACCINATION_EXISTS",
            "This vaccine is already scheduled at this age (reactivate it if it is inactive).",
            existing_id=duplicate,
        )


def assert_medicine_free(db: DbSession, payload: MedicineInput, medicine_id: int | None = None) -> None:
    duplicate = db.scalar(
        select(LayerMedicineProgram.id).where(
            func.lower(LayerMedicineProgram.medicine_name) == payload.medicine_name.lower(),
            LayerMedicineProgram.id != (medicine_id or 0),
        )
    )
    if duplicate:
        raise conflict(
            "LAYER_MEDICINE_EXISTS",
            "This medicine already has a program (reactivate it if it is inactive).",
            existing_id=duplicate,
        )


# --- Vaccination schedule ----------------------------------------------------------------------


@router.get("/layer-vaccinations")
def list_vaccinations(
    _: ActiveUser,
    db: DbSession,
    is_active: bool | None = None,
    age_week: Annotated[int | None, Query(ge=0, le=MAX_AGE_WEEK)] = None,
    q: Annotated[str | None, Query(max_length=100, description="Vaccine name or target disease.")] = None,
) -> dict[str, object]:
    """The whole vaccination schedule, youngest age first (not paginated: it is one program)."""
    stmt = select(LayerVaccination)
    if is_active is not None:
        stmt = stmt.where(LayerVaccination.is_active.is_(is_active))
    if age_week is not None:
        stmt = stmt.where(LayerVaccination.age_week == age_week)
    if q:
        stmt = stmt.where(
            or_(LayerVaccination.vaccine_name.ilike(f"%{q}%"), LayerVaccination.target_disease.ilike(f"%{q}%"))
        )
    rows = db.scalars(stmt.order_by(*VACCINATION_ORDER)).all()
    return {"data": [vaccination_out(row) for row in rows]}


# --- Vaccination schedule: Excel template and import -------------------------------------------

# pg_advisory_xact_lock key: two imports never interleave.
IMPORT_LOCK = 20261127
TEXT_COLUMNS = ("vaccine_name", "target_disease", "dosage", "notes")


@router.get(
    "/layer-vaccinations/template",
    response_class=Response,
    responses={200: {"content": {MEDIA_TYPE: {}}, "description": "The .xlsx template."}},
)
def vaccination_template(
    _: ActiveUser,
    db: DbSession,
    include_current: Annotated[
        bool, Query(description="Prefill the sheet with the current active schedule (false: empty sheet).")
    ] = True,
) -> Response:
    """The Excel template of the vaccination schedule: a ``Vaccinations`` sheet with the columns
    of the schedule (drop-down for ``method``, 0-200 check on ``age_week``) and an ``Instructions``
    sheet. Prefilled with the current active schedule, so it can be edited and uploaded again."""
    rows = (
        db.scalars(
            select(LayerVaccination).where(LayerVaccination.is_active.is_(True)).order_by(*VACCINATION_ORDER)
        ).all()
        if include_current
        else []
    )
    content = template([{column: getattr(row, column) for column in COLUMNS} for row in rows])
    return Response(
        content=content,
        media_type=MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{FILE_NAME}"'},
    )


def cell_input(row: SheetRow) -> dict[str, object]:
    """Excel cell values as the API would receive them."""
    values = dict(row.values)
    for column in TEXT_COLUMNS:
        value = values.get(column)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if value is not None and not isinstance(value, str):
            values[column] = str(value)
    method = values.get("method")
    if isinstance(method, str):
        values["method"] = method.upper().replace(" ", "_").replace("-", "_")
    if isinstance(values.get("age_week"), bool):  # TRUE/FALSE is not an age
        values["age_week"] = str(values["age_week"])
    return values


def parse_schedule(rows: list[SheetRow]) -> list[VaccinationInput]:
    """Validates every row with the rules of the single-item endpoint; all errors at once, keyed
    ``rows.<Excel row number>.<column>``."""
    errors: dict[str, list[str]] = {}
    items: list[VaccinationInput] = []
    seen: dict[tuple[int, str], int] = {}
    for row in rows:
        try:
            item = VaccinationInput.model_validate(cell_input(row))
        except ValidationError as exc:
            for error in exc.errors():
                field = str(error["loc"][0]) if error["loc"] else "row"
                errors.setdefault(f"rows.{row.number}.{field}", []).append(error["msg"])
            continue
        key = (item.age_week, item.vaccine_name.lower())
        if key in seen:
            errors.setdefault(f"rows.{row.number}.vaccine_name", []).append(
                f"The same vaccine and age week as row {seen[key]}."
            )
            continue
        seen[key] = row.number
        items.append(item)
    if errors:
        raise AppError(422, "VALIDATION_ERROR", "The file has invalid rows; nothing was imported.", errors=errors)
    return items


def replace_schedule(
    db: Session, items: list[VaccinationInput], actor: User, source: str, *, dry_run: bool
) -> dict[str, object]:
    """Makes the active schedule exactly ``items``.

    A row with the age week and vaccine name of an existing vaccination (active or not) updates and
    keeps that vaccination: its id stays, so what the ABK already recorded for it (this week's
    snooze, activities) still applies. Other rows are created. Every active vaccination not in
    ``items`` is deactivated: it is no longer valid and stops being reminded from the next reminder
    run. Nothing is deleted. With ``dry_run`` nothing is written."""
    db.execute(text("select pg_advisory_xact_lock(:key)"), {"key": IMPORT_LOCK})
    existing = db.scalars(select(LayerVaccination).order_by(LayerVaccination.id).with_for_update()).all()
    by_key = {(row.age_week, row.vaccine_name.lower()): row for row in existing}
    schedule: list[dict[str, object]] = []
    kept: set[int] = set()
    for item in items:
        values = vaccination_values(item)
        row = by_key.get((item.age_week, item.vaccine_name.lower()))
        if row is None:
            change = "CREATED"
        elif row.is_active and all(getattr(row, key) == value for key, value in values.items()):
            change = "UNCHANGED"
        else:
            change = "UPDATED"
        if row is not None:
            kept.add(row.id)
        if not dry_run and change == "CREATED":
            row = LayerVaccination(**values, is_active=True)
            db.add(row)
            db.flush()
            audit(db, row, "layer_vaccination_imported", actor, reason=source)
        elif not dry_run and change == "UPDATED":
            assert row is not None
            before = model_snapshot(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.is_active = True
            audit(db, row, "layer_vaccination_import_updated", actor, before=before, reason=source)
        schedule.append({"id": row.id if row else None, **values, "change": change})
    deactivated = [row for row in existing if row.is_active and row.id not in kept]
    deactivated_out = [
        {"id": row.id, "age_week": row.age_week, "vaccine_name": row.vaccine_name} for row in deactivated
    ]
    if not dry_run:
        for row in deactivated:
            before = model_snapshot(row)
            row.is_active = False
            audit(db, row, "layer_vaccination_import_deactivated", actor, before=before, reason=source)
    schedule.sort(key=lambda entry: (entry["age_week"], str(entry["vaccine_name"]).lower()))
    counts = {change: sum(entry["change"] == change for entry in schedule) for change in ("CREATED", "UPDATED")}
    return {
        "dry_run": dry_run,
        "summary": {
            "rows": len(items),
            "created": counts["CREATED"],
            "updated": counts["UPDATED"],
            "unchanged": len(items) - counts["CREATED"] - counts["UPDATED"],
            "deactivated": len(deactivated),
        },
        "schedule": schedule,
        "deactivated": deactivated_out,
    }


@router.post("/layer-vaccinations/import")
def import_vaccinations(
    actor: ActiveUser,
    db: DbSession,
    file: Annotated[UploadFile, File(description="The filled-in template (.xlsx, up to 2 MB).")],
    dry_run: Annotated[bool, Query(description="Only check the file and show what would change.")] = False,
) -> dict[str, object]:
    """Replaces the whole vaccination schedule with the rows of the uploaded template.

    The file is read and validated here (same rules as ``POST /layer-vaccinations``, at most 500
    rows, no duplicate vaccine per age week). Any invalid row rejects the whole file with
    ``422`` and every error keyed ``rows.<Excel row>.<column>``; nothing changes. Otherwise, in one
    transaction, the file becomes the active schedule: matching vaccinations (same age week and
    name) are updated and keep their id, new ones are created, and every other active vaccination is
    deactivated (no longer valid). The reminders follow at the next nightly run.

    ``dry_run=true`` answers the same without changing anything, to show it before confirming."""
    content = file.file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise AppError(413, "FILE_TOO_LARGE", "The file is larger than 2 MB.")
    items = parse_schedule(read_rows(content))
    source = f"import:{(file.filename or 'upload.xlsx')[:200]}"
    result = replace_schedule(db, items, actor, source, dry_run=dry_run)
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return {"data": result}


@router.get("/layer-vaccinations/{vaccination_id}")
def get_vaccination(vaccination_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(LayerVaccination, vaccination_id)
    if not row:
        raise not_found("Layer vaccination")
    return {"data": vaccination_out(row)}


@router.post("/layer-vaccinations", status_code=status.HTTP_201_CREATED)
def create_vaccination(payload: VaccinationInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    assert_vaccination_free(db, payload)
    row = LayerVaccination(**vaccination_values(payload), is_active=True)
    db.add(row)
    db.flush()
    audit(db, row, "layer_vaccination_created", actor)
    db.commit()
    return {"data": vaccination_out(row)}


@router.patch("/layer-vaccinations/{vaccination_id}")
def update_vaccination(
    vaccination_id: int, payload: VaccinationUpdate, actor: ActiveUser, db: DbSession
) -> dict[str, object]:
    """Replaces every field (send the full item). Changing the age moves the vaccination in the schedule."""
    row = db.scalar(select(LayerVaccination).where(LayerVaccination.id == vaccination_id).with_for_update())
    if not row:
        raise not_found("Layer vaccination")
    assert_vaccination_free(db, payload, row.id)
    before = model_snapshot(row)
    for key, value in vaccination_values(payload).items():
        setattr(row, key, value)
    row.is_active = payload.is_active
    audit(db, row, "layer_vaccination_updated", actor, before=before)
    db.commit()
    return {"data": vaccination_out(row)}


# --- Medicine program --------------------------------------------------------------------------


@router.get("/layer-medicine-programs")
def list_medicines(
    _: ActiveUser,
    db: DbSession,
    is_active: bool | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Medicine name or purpose.")] = None,
) -> dict[str, object]:
    """Every medicine program, by the age of its first course."""
    stmt = select(LayerMedicineProgram)
    if is_active is not None:
        stmt = stmt.where(LayerMedicineProgram.is_active.is_(is_active))
    if q:
        stmt = stmt.where(
            or_(LayerMedicineProgram.medicine_name.ilike(f"%{q}%"), LayerMedicineProgram.purpose.ilike(f"%{q}%"))
        )
    rows = db.scalars(stmt.order_by(*MEDICINE_ORDER)).all()
    return {"data": [medicine_out(row) for row in rows]}


@router.get("/layer-medicine-programs/{medicine_id}")
def get_medicine(medicine_id: int, _: ActiveUser, db: DbSession) -> dict[str, object]:
    row = db.get(LayerMedicineProgram, medicine_id)
    if not row:
        raise not_found("Layer medicine program")
    return {"data": medicine_out(row)}


@router.post("/layer-medicine-programs", status_code=status.HTTP_201_CREATED)
def create_medicine(payload: MedicineInput, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    assert_medicine_free(db, payload)
    row = LayerMedicineProgram(**medicine_values(payload), is_active=True)
    db.add(row)
    db.flush()
    audit(db, row, "layer_medicine_program_created", actor)
    db.commit()
    return {"data": medicine_out(row)}


@router.patch("/layer-medicine-programs/{medicine_id}")
def update_medicine(medicine_id: int, payload: MedicineUpdate, actor: ActiveUser, db: DbSession) -> dict[str, object]:
    """Replaces every field (send the full item)."""
    row = db.scalar(select(LayerMedicineProgram).where(LayerMedicineProgram.id == medicine_id).with_for_update())
    if not row:
        raise not_found("Layer medicine program")
    assert_medicine_free(db, payload, row.id)
    before = model_snapshot(row)
    for key, value in medicine_values(payload).items():
        setattr(row, key, value)
    row.is_active = payload.is_active
    audit(db, row, "layer_medicine_program_updated", actor, before=before)
    db.commit()
    return {"data": medicine_out(row)}


# --- Reminders ---------------------------------------------------------------------------------


@router.get("/layer-health-reminders")
def staff_reminders(
    _: ActiveUser,
    db: DbSession,
    from_date: Annotated[date | None, Query(alias="from", description="Default: today.")] = None,
    to_date: Annotated[date | None, Query(alias="to", description="Default: 7 days after from.")] = None,
    group_id: int | None = None,
    include_snoozed: bool = Query(default=False, description="Also list the snoozed (recorded) tasks."),
) -> dict[str, object]:
    """The reminders stored by the nightly job, per date and group, over any period up to 92 days:
    the coming week (today and the next 7 days, refreshed every night) and the earlier dates as they
    were last computed. Groups closed since the last run are still listed until the next run."""
    start = from_date or today_local()
    end = to_date or start + timedelta(days=REMINDER_DAYS)
    if end < start:
        raise validation("to", "The end date cannot be before the start date.")
    if (end - start).days >= MAX_REMINDER_RANGE_DAYS:
        raise validation("to", f"The period can cover at most {MAX_REMINDER_RANGE_DAYS} days.")
    if group_id is not None and not db.get(LivestockGroup, group_id):
        raise validation("group_id", "Group does not exist.")
    return {"data": stored_reminders(db, start, end, group_id=group_id, include_snoozed=include_snoozed)}
