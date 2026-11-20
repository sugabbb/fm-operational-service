"""Hen age on livestock groups, the layer health schedule rules, the nightly reminder job, and the
reminder APIs (which only read what the job stored)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.security import hash_password
from app.db.models import (
    AbkAccount,
    AuditEvent,
    Employee,
    LayerHealthDailyReminder,
    LayerHealthDailyReminderTask,
    LayerMedicineProgram,
    LayerVaccination,
    LivestockGroup,
    User,
)
from app.db.session import SessionLocal
from app.features import abk, layer_health, layer_health_reminders
from app.features import farm as farm_feature
from app.features.layer_health_reminders import RebuildResult, rebuild
from app.features.layer_health_schedule import Age, age_on, medicine_course_day, vaccination_window_day
from app.main import app
from app.scripts import layer_health_reminder_job

PASSWORD = "StrongPassword123"
TODAY = date(2026, 3, 2)
ABK_REMINDERS = "/api/v1/abk/layer-health-reminders"
STAFF_REMINDERS = "/api/v1/layer-health-reminders"


# --- Schedule rules (no database) --------------------------------------------------------------


def hens(start: date = TODAY, arrival_age_week: int | None = 13) -> LivestockGroup:
    return LivestockGroup(id=1, species="AYAM", start_date=start, arrival_age_week=arrival_age_week)


def program(start_age_week: int, start_day: int, course_days: int, every: int, unit: str) -> LayerMedicineProgram:
    return LayerMedicineProgram(
        id=1,
        start_age_week=start_age_week,
        start_day=start_day,
        course_days=course_days,
        interval_value=every,
        interval_unit=unit,
    )


def course_days_on(medicine: LayerMedicineProgram, group: LivestockGroup, days: int) -> dict[int, int]:
    """{days after the start date: course day} over the first ``days`` days of the group."""
    result = {}
    for offset in range(days):
        age = age_on(group, group.start_date + timedelta(days=offset))
        assert age is not None
        course_day = medicine_course_day(medicine, age)
        if course_day is not None:
            result[offset] = course_day
    return result


def test_age_starts_at_the_arrival_age_and_counts_days() -> None:
    group = hens(arrival_age_week=13)
    assert age_on(group, TODAY) == Age(13, 1)
    assert age_on(group, TODAY + timedelta(days=6)) == Age(13, 7)
    assert age_on(group, TODAY + timedelta(days=7)) == Age(14, 1)
    assert age_on(group, TODAY + timedelta(days=100)) == Age(27, 3)
    assert age_on(group, TODAY - timedelta(days=1)) is None  # not arrived yet
    assert age_on(hens(arrival_age_week=0), TODAY) == Age(0, 1)  # day-old chicks
    assert age_on(hens(arrival_age_week=None), TODAY) is None  # Lele


def test_vaccination_is_due_on_any_day_of_its_age_week() -> None:
    vaccination = LayerVaccination(age_week=14)
    assert [vaccination_window_day(vaccination, Age(14, day)) for day in range(1, 8)] == [1, 2, 3, 4, 5, 6, 7]
    assert vaccination_window_day(vaccination, Age(13, 7)) is None
    assert vaccination_window_day(vaccination, Age(15, 1)) is None


def test_medicine_courses_follow_the_age() -> None:
    group = hens(arrival_age_week=13)
    # Day 3 of week 13, 3 days, every week: same days of every age week.
    weekly = program(13, 3, 3, 1, "WEEK")
    assert course_days_on(weekly, group, 16) == {2: 1, 3: 2, 4: 3, 9: 1, 10: 2, 11: 3}
    # Every 10 days, single day, from day 1 of week 13.
    assert course_days_on(program(13, 1, 1, 10, "DAY"), group, 31) == {0: 1, 10: 1, 20: 1, 30: 1}
    # A month is 4 age weeks, so the course stays on the same day of the age week.
    monthly = program(13, 2, 2, 1, "MONTH")
    assert course_days_on(monthly, group, 60) == {1: 1, 2: 2, 29: 1, 30: 2, 57: 1, 58: 2}
    # Daily.
    assert course_days_on(program(13, 1, 1, 1, "DAY"), group, 3) == {0: 1, 1: 1, 2: 1}


def test_medicine_started_before_arrival_keeps_its_rhythm() -> None:
    group = hens(arrival_age_week=13)  # arrives at 91 days of age
    # Started day 5 of week 12 (88 days), 7 days every 2 weeks: at arrival it is on its day 4.
    assert course_days_on(program(12, 5, 7, 2, "WEEK"), group, 15) == {
        0: 4,
        1: 5,
        2: 6,
        3: 7,
        11: 1,
        12: 2,
        13: 3,
        14: 4,
    }
    # Started week 8 (56 days), every month: courses at 84 days (before arrival) and 112 (21 days after).
    assert course_days_on(program(8, 1, 5, 1, "MONTH"), group, 28) == {21: 1, 22: 2, 23: 3, 24: 4, 25: 5}


# --- Fixtures ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Farm:
    k1: int  # arrives today at 13 weeks
    k2: int  # arrived 8 weeks ago at 16 weeks: 24 weeks today
    k3: int  # day-old chicks arriving today: nothing in the program yet
    k4: int  # arrives in 3 days
    closed: int
    lele: int
    employee: int


class Clock:
    """today_local() of the code under test; move it to simulate the following days."""

    def __init__(self) -> None:
        self.today = TODAY

    def __call__(self) -> date:
        return self.today


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    for module in (abk, farm_feature, layer_health, layer_health_reminders):
        monkeypatch.setattr(module, "today_local", clock)
    return clock


@pytest.fixture
def farm(clean_database: None, clock: Clock) -> Farm:
    with SessionLocal() as db:
        db.add(
            User(
                name="Admin",
                email="admin@example.com",
                password_hash=hash_password(PASSWORD),
                role="ADMIN",
                is_active=True,
                must_change_password=False,
            )
        )

        def group(code: str, species: str = "AYAM", **values: Any) -> LivestockGroup:
            defaults: dict[str, Any] = {
                "name": code.replace("K", "Kandang "),
                "start_date": TODAY,
                "initial_population": 100,
                "location": "Blok A",
                "status": "ACTIVE",
            }
            return LivestockGroup(species=species, code=code, **(defaults | values))

        k1 = group("K1", arrival_age_week=13)
        k2 = group("K2", arrival_age_week=16, start_date=TODAY - timedelta(weeks=8))
        k3 = group("K3", arrival_age_week=0)
        k4 = group("K4", arrival_age_week=13, start_date=TODAY + timedelta(days=3))
        closed = group("K9", arrival_age_week=13, status="CLOSED", closed_at=TODAY + timedelta(days=30))
        lele = group("L1", species="LELE", name="Kolam 1")
        employee = Employee(name="Budi", job_title="ABK", joined_at=date(2025, 1, 1), status="ACTIVE")
        db.add_all([k1, k2, k3, k4, closed, lele, employee])
        db.add_all(
            [
                LayerVaccination(
                    age_week=13, vaccine_name="Coryza", target_disease="Snot", method="INTRAMUSCULAR", is_active=True
                ),
                LayerVaccination(
                    age_week=14, vaccine_name="ND-IB Live", method="EYE_DROP", dosage="1 tetes/ekor", is_active=True
                ),
                LayerVaccination(age_week=10, vaccine_name="Gumboro", method="DRINKING_WATER", is_active=True),
                LayerVaccination(age_week=13, vaccine_name="AI Killed", method="SUBCUTANEOUS", is_active=False),
                LayerVaccination(age_week=24, vaccine_name="ND Booster", method="INTRAMUSCULAR", is_active=True),
                LayerMedicineProgram(
                    medicine_name="Vitamin",
                    purpose="Stres",
                    method="DRINKING_WATER",
                    dosage="1 g / 2 L",
                    start_age_week=13,
                    start_day=3,
                    course_days=3,
                    interval_value=1,
                    interval_unit="WEEK",
                    notes="Berikan pagi hari",
                    is_active=True,
                ),
                LayerMedicineProgram(
                    medicine_name="Obat Cacing",
                    method="FEED",
                    start_age_week=8,
                    start_day=1,
                    course_days=5,
                    interval_value=1,
                    interval_unit="MONTH",
                    is_active=True,
                ),
                LayerMedicineProgram(
                    medicine_name="Antikoksi",
                    method="DRINKING_WATER",
                    start_age_week=12,
                    start_day=5,
                    course_days=7,
                    interval_value=2,
                    interval_unit="WEEK",
                    is_active=True,
                ),
                LayerMedicineProgram(
                    medicine_name="Lama",
                    method="OTHER",
                    start_age_week=0,
                    start_day=1,
                    course_days=1,
                    interval_value=1,
                    interval_unit="DAY",
                    is_active=False,
                ),
            ]
        )
        db.commit()
        return Farm(k1.id, k2.id, k3.id, k4.id, closed.id, lele.id, employee.id)


def run_job(on: date = TODAY) -> RebuildResult:
    with SessionLocal() as db:
        return rebuild(db, on)


@pytest.fixture
def client(farm: Farm) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def abk_headers(client: TestClient, admin: dict[str, str], farm: Farm, scopes: list[str]) -> dict[str, str]:
    response = client.post(
        "/api/v1/abk-accounts",
        headers=admin,
        json={"employee_id": farm.employee, "username": "budi", "scopes": scopes},
    )
    assert response.status_code == 201, response.text
    return {"X-ABK-Username": "budi", "X-ABK-PIN": response.json()["data"]["pin"]}


def summary(data: dict[str, Any]) -> dict[str, dict[str, list[tuple[Any, ...]]]]:
    """{date: {group code: [(type, name, day of the vaccination week or of the course), ...]}}."""
    return {
        day["date"]: {
            entry["group"]["code"]: [
                (task["type"], task["name"], (task["window"] or task["course"])["day"]) for task in entry["tasks"]
            ]
            for entry in day["groups"]
        }
        for day in data["days"]
    }


def names_on(data: dict[str, Any], on: str, code: str) -> list[str] | None:
    """Task names of a group on a date; None when the group is not listed that date."""
    return [name for _, name, _ in summary(data)[on][code]] if code in summary(data)[on] else None


# --- The nightly job ---------------------------------------------------------------------------


def test_job_stores_every_active_group_for_today_and_the_next_7_days(farm: Farm) -> None:
    result = run_job()
    # K1, K2, K3 on 8 dates; K4 from its start date (5 dates). Not the closed or the Lele group.
    assert (result.start, result.end, result.groups, result.reminders) == (
        TODAY,
        TODAY + timedelta(days=7),
        4,
        8 * 3 + 5,
    )
    with SessionLocal() as db:
        rows = db.scalars(select(LayerHealthDailyReminder)).all()
        assert {row.group_id for row in rows} == {farm.k1, farm.k2, farm.k3, farm.k4}
        k3_rows = [row for row in rows if row.group_id == farm.k3]
        assert len(k3_rows) == 8 and all(row.tasks == [] for row in k3_rows)  # stored without any task
        assert min(row.reminder_date for row in rows if row.group_id == farm.k4) == TODAY + timedelta(days=3)
        assert db.scalar(select(func.count()).select_from(LayerHealthDailyReminderTask)) == result.tasks
    assert run_job() == result  # idempotent: the same dates are replaced, never duplicated


def test_job_refreshes_the_coming_week_and_keeps_earlier_days(
    client: TestClient, admin: dict[str, str], farm: Farm, clock: Clock
) -> None:
    run_job()
    with SessionLocal() as db:
        coryza = db.scalar(select(LayerVaccination).where(LayerVaccination.vaccine_name == "Coryza"))
        assert coryza is not None
    body = {key: getattr(coryza, key) for key in ("age_week", "vaccine_name", "target_disease", "method", "dosage")}
    client.patch(
        f"/api/v1/layer-vaccinations/{coryza.id}", headers=admin, json=body | {"notes": None, "is_active": False}
    )
    group = {"name": "Kandang 3", "start_date": TODAY.isoformat(), "initial_population": 100, "location": "Blok A"}
    client.patch(f"/api/v1/livestock-groups/{farm.k3}", headers=admin, json=group | {"arrival_age_week": 13})
    staff = client.get(STAFF_REMINDERS, headers=admin, params={"to": "2026-03-04"}).json()["data"]
    # Nothing changes before the next run.
    assert names_on(staff, "2026-03-03", "K1") == ["Coryza", "Antikoksi"]
    assert names_on(staff, "2026-03-03", "K3") == []

    clock.today = TODAY + timedelta(days=1)  # the next night
    result = run_job(clock.today)
    assert (result.start, result.end) == (date(2026, 3, 3), date(2026, 3, 10))
    staff = client.get(STAFF_REMINDERS, headers=admin, params={"from": "2026-03-02", "to": "2026-03-10"}).json()
    # The coming week follows the new program and ages; 2026-03-02 stays as the ABK saw it.
    assert names_on(staff["data"], "2026-03-02", "K1") == ["Coryza", "Antikoksi"]
    assert names_on(staff["data"], "2026-03-03", "K1") == ["Antikoksi"]
    assert names_on(staff["data"], "2026-03-02", "K3") == []
    assert names_on(staff["data"], "2026-03-03", "K3") == ["Antikoksi"]
    # The new last date of the window.
    assert names_on(staff["data"], "2026-03-10", "K1") == ["ND-IB Live"]
    computed = {day["date"]: {entry["computed_at"] for entry in day["groups"]} for day in staff["data"]["days"]}
    assert computed["2026-03-02"].isdisjoint(computed["2026-03-03"])


def test_closed_and_new_groups(client: TestClient, admin: dict[str, str], farm: Farm, clock: Clock) -> None:
    run_job()
    headers = abk_headers(client, admin, farm, ["AYAM"])
    assert client.post(f"/api/v1/livestock-groups/{farm.k2}/close", headers=admin, json={"closed_at": "2026-03-02"})
    new = client.post(
        "/api/v1/livestock-groups",
        headers=admin,
        json={
            "species": "AYAM",
            "name": "Kandang 5",
            "start_date": "2026-03-02",
            "initial_population": 10,
            "location": "Blok E",
            "arrival_age_week": 24,
        },
    ).json()["data"]
    # Before the next run the ABK no longer see the closed group; staff still see what was stored.
    abk_view = client.get(ABK_REMINDERS, headers=headers).json()["data"]
    assert all("K2" not in groups for groups in summary(abk_view).values())
    staff = client.get(STAFF_REMINDERS, headers=admin).json()["data"]
    assert "K2" in summary(staff)["2026-03-03"]
    assert all(new["code"] not in groups for groups in summary(abk_view).values())
    # The next run drops the closed group's coming days and adds the new group.
    clock.today = TODAY + timedelta(days=1)
    run_job(clock.today)
    staff = client.get(STAFF_REMINDERS, headers=admin, params={"from": "2026-03-02"}).json()["data"]
    assert "K2" in summary(staff)["2026-03-02"] and "K2" not in summary(staff)["2026-03-03"]
    assert new["code"] not in summary(staff)["2026-03-02"] and new["code"] in summary(staff)["2026-03-03"]
    assert names_on(staff, "2026-03-03", new["code"]) == ["ND Booster", "Obat Cacing"]


def test_job_script(farm: Farm, capsys: pytest.CaptureFixture[str]) -> None:
    assert layer_health_reminder_job.main(["--date", "2026-03-02"]) == 0
    assert "2026-03-02..2026-03-09: 4 active groups, 29 group-days" in capsys.readouterr().out
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LayerHealthDailyReminder)) == 29
    with pytest.raises(SystemExit):
        layer_health_reminder_job.main(["--date", "tomorrow"])


# --- ABK reminders -----------------------------------------------------------------------------


def test_abk_sees_today_and_the_next_days(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    run_job()
    headers = abk_headers(client, admin, farm, ["AYAM"])
    response = client.get(ABK_REMINDERS, headers=headers, params={"group_id": farm.k1})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["today"], data["from"], data["to"]) == ("2026-03-02", "2026-03-02", "2026-03-09")
    assert [(day["date"], day["days_from_today"]) for day in data["days"]][:2] == [
        ("2026-03-02", 0),
        ("2026-03-03", 1),
    ]
    # Coryza (week 13) can be given any day of the week the hens arrive in; ND-IB Live on any day of
    # week 14. Gumboro (week 10) was before arrival; AI Killed and "Lama" are inactive; Obat Cacing's
    # next course is at 16 weeks (2026-03-23).
    coryza = [("VACCINATION", "Coryza", day) for day in range(1, 8)]
    assert summary(data) == {
        "2026-03-02": {"K1": [coryza[0], ("MEDICINE", "Antikoksi", 4)]},
        "2026-03-03": {"K1": [coryza[1], ("MEDICINE", "Antikoksi", 5)]},
        "2026-03-04": {"K1": [coryza[2], ("MEDICINE", "Antikoksi", 6), ("MEDICINE", "Vitamin", 1)]},
        "2026-03-05": {"K1": [coryza[3], ("MEDICINE", "Antikoksi", 7), ("MEDICINE", "Vitamin", 2)]},
        "2026-03-06": {"K1": [coryza[4], ("MEDICINE", "Vitamin", 3)]},
        "2026-03-07": {"K1": [coryza[5]]},
        "2026-03-08": {"K1": [coryza[6]]},
        "2026-03-09": {"K1": [("VACCINATION", "ND-IB Live", 1)]},
    }
    last = data["days"][7]["groups"][0]
    assert last["group"] == {"id": farm.k1, "code": "K1", "name": "Kandang 1", "location": "Blok A"}
    assert last["age"] == {"week": 14, "day": 1}
    with SessionLocal() as db:
        vaccine_id = db.scalar(select(LayerVaccination.id).where(LayerVaccination.vaccine_name == "ND-IB Live"))
    assert last["tasks"] == [
        {
            "key": f"VACCINATION-{vaccine_id}-{farm.k1}-2026-03-09",
            "type": "VACCINATION",
            "source_id": vaccine_id,
            "name": "ND-IB Live",
            "target_disease": None,
            "purpose": None,
            "method": "EYE_DROP",
            "dosage": "1 tetes/ekor",
            "notes": None,
            "window": {"from": "2026-03-09", "to": "2026-03-15", "day": 1, "days": 7},
            "course": None,
            "snoozed_at": None,
        }
    ]
    on_0304 = data["days"][2]["groups"][0]
    assert on_0304["age"] == {"week": 13, "day": 3}
    coryza_task, _, vitamin = on_0304["tasks"]
    assert coryza_task["window"] == {"from": "2026-03-02", "to": "2026-03-08", "day": 3, "days": 7}
    assert coryza_task["target_disease"] == "Snot"
    assert (vitamin["course"], vitamin["window"]) == ({"day": 1, "days": 3}, None)
    assert (vitamin["purpose"], vitamin["dosage"], vitamin["notes"]) == ("Stres", "1 g / 2 L", "Berikan pagi hari")


def test_abk_reminders_list_every_active_group(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    run_job()
    headers = abk_headers(client, admin, farm, ["AYAM"])
    data = client.get(ABK_REMINDERS, headers=headers, params={"days": 0}).json()["data"]
    assert [day["date"] for day in data["days"]] == ["2026-03-02"]
    # By group name, also K3 without any task (K4 arrives later). Per group vaccinations first,
    # then medicines. K2 is 24 weeks old today and starts a monthly deworming course
    # (8 weeks + 4 x 4 weeks).
    assert summary(data)["2026-03-02"] == {
        "K1": [("VACCINATION", "Coryza", 1), ("MEDICINE", "Antikoksi", 4)],
        "K2": [("VACCINATION", "ND Booster", 1), ("MEDICINE", "Obat Cacing", 1)],
        "K3": [],
    }
    assert [entry["age"] for entry in data["days"][0]["groups"]] == [
        {"week": 13, "day": 1},
        {"week": 24, "day": 1},
        {"week": 0, "day": 1},
    ]
    week = client.get(ABK_REMINDERS, headers=headers).json()["data"]
    assert list(summary(week)["2026-03-05"]) == ["K1", "K2", "K3", "K4"]


def test_abk_reminders_before_the_first_run_are_empty(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    headers = abk_headers(client, admin, farm, ["AYAM"])
    data = client.get(ABK_REMINDERS, headers=headers).json()["data"]
    assert len(data["days"]) == 8 and all(day["groups"] == [] for day in data["days"])


def test_abk_reminders_access(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    run_job()
    assert client.get(ABK_REMINDERS).status_code == 401
    assert client.get(ABK_REMINDERS, headers=admin).status_code == 401
    headers = abk_headers(client, admin, farm, ["LELE"])
    forbidden = client.get(ABK_REMINDERS, headers=headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ABK_SCOPE_FORBIDDEN" and forbidden.json()["scopes"] == ["LELE"]
    with SessionLocal() as db:
        account_id = db.scalar(select(AbkAccount.id))
    client.patch(
        f"/api/v1/abk-accounts/{account_id}",
        headers=admin,
        json={"username": "budi", "is_active": True, "scopes": ["AYAM"]},
    )
    assert client.get(ABK_REMINDERS, headers=headers).status_code == 200
    # At most 7 days ahead.
    assert len(client.get(ABK_REMINDERS, headers=headers, params={"days": 7}).json()["data"]["days"]) == 8
    for params in ({"days": -1}, {"days": 8}):
        assert client.get(ABK_REMINDERS, headers=headers, params=params).status_code == 422
    for group_id in (farm.lele, farm.closed, 999):
        response = client.get(ABK_REMINDERS, headers=headers, params={"group_id": group_id})
        assert response.status_code == 422 and "group_id" in response.json()["errors"]


# --- Staff reminders ---------------------------------------------------------------------------


def test_staff_reminders_read_the_stored_days(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    run_job()
    default = client.get(STAFF_REMINDERS, headers=admin).json()["data"]
    assert (default["from"], default["to"]) == ("2026-03-02", "2026-03-09")
    assert summary(default) == summary(
        client.get(ABK_REMINDERS, headers=abk_headers(client, admin, farm, ["AYAM"]), params={"days": 7}).json()["data"]
    )
    # Beyond the stored week there is nothing (no computation on request).
    later = client.get(
        STAFF_REMINDERS, headers=admin, params={"from": "2026-03-08", "to": "2026-03-12", "group_id": farm.k1}
    ).json()["data"]
    assert [len(day["groups"]) for day in later["days"]] == [1, 1, 0, 0, 0]
    assert later["days"][0]["days_from_today"] == 6
    unknown = client.get(STAFF_REMINDERS, headers=admin, params={"group_id": 999})
    assert unknown.status_code == 422


@pytest.mark.parametrize(
    "params",
    [{"from": "2026-03-10", "to": "2026-03-09"}, {"from": "2026-03-01", "to": "2026-06-01"}],
)
def test_staff_reminders_period_limits(
    client: TestClient, admin: dict[str, str], farm: Farm, params: dict[str, str]
) -> None:
    response = client.get(STAFF_REMINDERS, headers=admin, params=params)
    assert response.status_code == 422 and "to" in response.json()["errors"]
    assert (
        client.get(STAFF_REMINDERS, headers=admin, params={"from": "2026-03-01", "to": "2026-05-31"}).status_code == 200
    )


# --- Livestock groups: hen age -----------------------------------------------------------------


def group_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "species": "AYAM",
        "name": "Kandang Baru",
        "start_date": "2026-02-16",
        "initial_population": 500,
        "location": "Blok D",
    }
    return body | changes


def test_group_arrival_age_defaults_to_13_weeks(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    created = client.post("/api/v1/livestock-groups", headers=admin, json=group_body())
    assert created.status_code == 201, created.text
    # Arrived 14 days ago at 13 weeks.
    assert created.json()["data"]["arrival_age_week"] == 13
    assert created.json()["data"]["current_age"] == {"week": 15, "day": 1}
    explicit = client.post("/api/v1/livestock-groups", headers=admin, json=group_body(name="B", arrival_age_week=0))
    assert (explicit.json()["data"]["arrival_age_week"], explicit.json()["data"]["current_age"]) == (
        0,
        {"week": 2, "day": 1},
    )
    future = client.post("/api/v1/livestock-groups", headers=admin, json=group_body(name="C", start_date="2026-03-10"))
    assert future.json()["data"]["current_age"] is None  # not arrived yet
    # Groups inserted without an age (as before this change) get the default too.
    with SessionLocal() as db:
        assert db.get(LivestockGroup, farm.k1) is not None
        direct = LivestockGroup(
            species="AYAM",
            code="DIRECT",
            name="Direct",
            start_date=TODAY,
            initial_population=1,
            location="X",
            status="ACTIVE",
        )
        db.add(direct)
        db.commit()
        assert direct.arrival_age_week == 13


def test_group_arrival_age_rules(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    lele = client.post("/api/v1/livestock-groups", headers=admin, json=group_body(species="LELE", arrival_age_week=13))
    assert lele.status_code == 422 and "arrival_age_week" in lele.json()["errors"]
    lele = client.post("/api/v1/livestock-groups", headers=admin, json=group_body(species="LELE"))
    assert lele.status_code == 201
    assert (lele.json()["data"]["arrival_age_week"], lele.json()["data"]["current_age"]) == (None, None)
    for value in (-1, 201):
        response = client.post("/api/v1/livestock-groups", headers=admin, json=group_body(arrival_age_week=value))
        assert response.status_code == 422

    url = f"/api/v1/livestock-groups/{farm.k2}"
    k2 = {"name": "Kandang 2", "start_date": "2026-01-05", "initial_population": 100, "location": "Blok A"}
    unchanged = client.patch(url, headers=admin, json=k2)
    assert unchanged.json()["data"]["arrival_age_week"] == 16  # omitted: unchanged
    assert unchanged.json()["data"]["current_age"] == {"week": 24, "day": 1}
    changed = client.patch(url, headers=admin, json=k2 | {"arrival_age_week": 18})
    assert changed.json()["data"]["current_age"] == {"week": 26, "day": 1}
    cleared = client.patch(url, headers=admin, json=k2 | {"arrival_age_week": None})
    assert cleared.status_code == 422 and "arrival_age_week" in cleared.json()["errors"]
    lele_patch = client.patch(
        f"/api/v1/livestock-groups/{farm.lele}",
        headers=admin,
        json={"name": "Kolam 1", "start_date": "2026-03-02", "initial_population": 100, "location": "Blok A"}
        | {"arrival_age_week": 5},
    )
    assert lele_patch.status_code == 422
    with SessionLocal() as db:
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "group_updated").order_by(AuditEvent.id.desc())
        ).first()
    assert event is not None and event.before is not None and event.after is not None
    assert (event.before["arrival_age_week"], event.after["arrival_age_week"]) == (16, 18)
    # A closed group's age stops on its closing date.
    closed = client.get(f"/api/v1/livestock-groups/{farm.closed}", headers=admin).json()["data"]
    assert closed["current_age"] == {"week": 17, "day": 3}


def test_abk_form_options_show_the_hen_age(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    headers = abk_headers(client, admin, farm, ["AYAM", "LELE"])
    groups = client.get("/api/v1/abk/form-options", headers=headers).json()["data"]["livestock_groups"]
    ages = {group["code"]: (group["arrival_age_week"], group["current_age"]) for group in groups}
    assert ages == {
        "K1": (13, {"week": 13, "day": 1}),
        "K2": (16, {"week": 24, "day": 1}),
        "K3": (0, {"week": 0, "day": 1}),
        "K4": (13, None),  # arrives in 3 days
        "L1": (None, None),
    }
