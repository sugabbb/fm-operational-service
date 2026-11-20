"""When the layer health program falls due for a group: hen age and the daily task list.

Everything is timed by the hens' age, counted in days and shown as week and day of the week:

- On its ``start_date`` a group is ``arrival_age_week`` weeks old, on day 1 of that age week. Each
  day adds one day of age; every 7 days the age week goes up by one. Age week W, day D (1-7) is
  ``W * 7 + (D - 1)`` days of age.
- A vaccination is due during its whole ``age_week``: it can be given on any of those 7 days, so
  it is listed on each of them (``window_day`` 1-7).
- A medicine's first course starts on day ``start_day`` of age week ``start_age_week``. A course
  lasts ``course_days`` consecutive days and starts again every interval (DAY = 1 day, WEEK = 7
  days, MONTH = 4 age weeks = 28 days, so a course always starts on the same day of the age week).

Nothing is due before the group's start date: a vaccination at a younger age week than the arrival
age was given before the hens arrived. A medicine started before arrival keeps its age rhythm, so the
group joins it at its next course (or the rest of a course in progress).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from app.db.models import LayerMedicineProgram, LayerVaccination, LivestockGroup

INTERVAL_DAYS = {"DAY": 1, "WEEK": 7, "MONTH": 28}


@dataclass(frozen=True)
class Age:
    week: int
    day: int  # 1-7: day of the age week

    @property
    def days(self) -> int:
        return self.week * 7 + self.day - 1

    def out(self) -> dict[str, int]:
        return {"week": self.week, "day": self.day}


def age_on(group: LivestockGroup, on: date) -> Age | None:
    """The hens' age on a date; None before the group's start date or for a group without an age."""
    if group.arrival_age_week is None or on < group.start_date:
        return None
    days = group.arrival_age_week * 7 + (on - group.start_date).days
    return Age(days // 7, days % 7 + 1)


def interval_days(row: LayerMedicineProgram) -> int:
    return row.interval_value * INTERVAL_DAYS[row.interval_unit]


def first_course_age_days(row: LayerMedicineProgram) -> int:
    return Age(row.start_age_week, row.start_day).days


def vaccination_window_day(row: LayerVaccination, age: Age) -> int | None:
    """Day (1-7) of the vaccination's age week, or None outside that week."""
    return age.day if age.week == row.age_week else None


def medicine_course_day(row: LayerMedicineProgram, age: Age) -> int | None:
    """Day (1..course_days) of a course of this medicine at this age, or None on a day without one."""
    since_first = age.days - first_course_age_days(row)
    if since_first < 0:
        return None
    day = since_first % interval_days(row)
    return day + 1 if day < row.course_days else None


@dataclass(frozen=True)
class Task:
    """One thing to do for one group on one date."""

    on: date
    group: LivestockGroup
    vaccination: LayerVaccination | None = None
    window_day: int | None = None  # vaccination: day 1-7 of its age week
    medicine: LayerMedicineProgram | None = None
    course_day: int | None = None  # medicine: day 1..course_days of the course

    @property
    def key(self) -> str:
        """Stable identifier of the task: source, source id, group, and date."""
        if self.vaccination is not None:
            return f"VACCINATION-{self.vaccination.id}-{self.group.id}-{self.on.isoformat()}"
        assert self.medicine is not None
        return f"MEDICINE-{self.medicine.id}-{self.group.id}-{self.on.isoformat()}"

    @property
    def occurrence_key(self) -> str:
        """What is done once: a vaccination once in its age week (the same key on each of its 7
        days), a medicine once per course day (the task itself)."""
        if self.vaccination is not None:
            return f"VACCINATION-{self.vaccination.id}-{self.group.id}-{self.window[0].isoformat()}"
        return self.key

    @property
    def window(self) -> tuple[date, date]:
        """Vaccination: first and last date of its age week."""
        assert self.window_day is not None
        first = self.on - timedelta(days=self.window_day - 1)
        return first, first + timedelta(days=6)


@dataclass(frozen=True)
class GroupDay:
    """A group on one date: the hens' age and the tasks (possibly none)."""

    on: date
    group: LivestockGroup
    age: Age
    tasks: list[Task]


def group_day(
    on: date,
    group: LivestockGroup,
    vaccinations: Sequence[LayerVaccination],
    medicines: Sequence[LayerMedicineProgram],
) -> GroupDay | None:
    """The group's age and tasks on a date (vaccinations first, then medicines, each in the order
    given); None when the group has no age on that date (before its start date, after its closing
    date, or a Lele group)."""
    age = age_on(group, on)
    if age is None or (group.closed_at is not None and on > group.closed_at):
        return None
    tasks: list[Task] = []
    for vaccination in vaccinations:
        window_day = vaccination_window_day(vaccination, age)
        if window_day is not None:
            tasks.append(Task(on, group, vaccination=vaccination, window_day=window_day))
    for medicine in medicines:
        course_day = medicine_course_day(medicine, age)
        if course_day is not None:
            tasks.append(Task(on, group, medicine=medicine, course_day=course_day))
    return GroupDay(on, group, age, tasks)


def dates(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
