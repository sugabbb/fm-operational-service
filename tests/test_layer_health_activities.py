"""Layer health activities: the ABK record reminder tasks as done or skipped; the activity copies the
task, and the reminder context snoozes the task's occurrence."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import (
    AuditEvent,
    Employee,
    LayerHealthDailyActivity,
    LayerHealthReminderSnooze,
    LayerMedicineProgram,
    LayerVaccination,
    LivestockGroup,
    User,
)
from app.db.session import SessionLocal
from app.features import abk, farm, layer_health, layer_health_activities, layer_health_reminders
from app.features.layer_health_reminders import rebuild, snooze
from app.main import app

PASSWORD = "StrongPassword123"
TODAY = date(2026, 3, 2)
ACTIVITIES = "/api/v1/abk/layer-health-activities"
REMINDERS = "/api/v1/abk/layer-health-reminders"
STAFF_ACTIVITIES = "/api/v1/layer-health-activities"


class Clock:
    def __init__(self) -> None:
        self.today = TODAY

    def __call__(self) -> date:
        return self.today


@dataclass(frozen=True)
class Farm:
    k1: int
    k2: int
    coryza: int  # vaccination, week 13 (2026-03-02..08)
    nd: int  # vaccination, week 14 (2026-03-09..15)
    vitamin: int  # medicine, 3 days from day 1 of week 13, weekly
    budi: int
    citra: int
    dodi: int


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    for module in (abk, farm, layer_health, layer_health_reminders, layer_health_activities):
        monkeypatch.setattr(module, "today_local", clock)
    return clock


@pytest.fixture
def setup(clean_database: None, clock: Clock) -> Farm:
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
        groups = [
            LivestockGroup(
                species="AYAM",
                code=f"K{number}",
                name=f"Kandang {number}",
                start_date=TODAY,
                initial_population=100,
                arrival_age_week=13,
                location="Blok A",
                status="ACTIVE",
            )
            for number in (1, 2)
        ]
        coryza = LayerVaccination(
            age_week=13,
            vaccine_name="Coryza",
            target_disease="Snot",
            method="INTRAMUSCULAR",
            dosage="0,5 ml/ekor",
            notes="Suntik dada",
            is_active=True,
        )
        nd = LayerVaccination(age_week=14, vaccine_name="ND-IB Live", method="EYE_DROP", is_active=True)
        vitamin = LayerMedicineProgram(
            medicine_name="Vitamin",
            purpose="Stres",
            method="DRINKING_WATER",
            dosage="1 g / 2 L",
            start_age_week=13,
            start_day=1,
            course_days=3,
            interval_value=1,
            interval_unit="WEEK",
            is_active=True,
        )
        employees = [
            Employee(name=name, job_title="ABK", joined_at=date(2025, 1, 1), status="ACTIVE")
            for name in ("Budi", "Citra", "Dodi")
        ]
        db.add_all([*groups, coryza, nd, vitamin, *employees])
        db.commit()
        rebuild(db, TODAY)
        return Farm(groups[0].id, groups[1].id, coryza.id, nd.id, vitamin.id, *(row.id for row in employees))


@pytest.fixture
def client(setup: Farm) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
def abks(client: TestClient, admin: dict[str, str], setup: Farm) -> dict[str, dict[str, str]]:
    """ABK headers: budi and citra (chicken), dodi (fish only)."""
    headers = {}
    for username, employee_id, scopes in (
        ("budi", setup.budi, ["AYAM"]),
        ("citra", setup.citra, ["AYAM"]),
        ("dodi", setup.dodi, ["LELE"]),
    ):
        response = client.post(
            "/api/v1/abk-accounts",
            headers=admin,
            json={"employee_id": employee_id, "username": username, "scopes": scopes},
        )
        assert response.status_code == 201, response.text
        headers[username] = {"X-ABK-Username": username, "X-ABK-PIN": response.json()["data"]["pin"]}
    return headers


def key(kind: str, source_id: int, group_id: int, on: str) -> str:
    return f"{kind}-{source_id}-{group_id}-{on}"


def reminded(client: TestClient, headers: dict[str, str], **params: Any) -> dict[str, dict[str, list[str]]]:
    """{date: {group code: [task names]}} of the ABK reminders."""
    data = client.get(REMINDERS, headers=headers, params=params).json()["data"]
    return {
        day["date"]: {entry["group"]["code"]: [task["name"] for task in entry["tasks"]] for entry in day["groups"]}
        for day in data["days"]
    }


def record(client: TestClient, headers: dict[str, str], task_key: str, **body: Any) -> Any:
    return client.post(ACTIVITIES, headers=headers, json={"task_key": task_key, "status": "DONE"} | body)


# --- Recording ---------------------------------------------------------------------------------


def test_done_copies_the_task_and_snoozes_the_vaccination_week(
    client: TestClient, abks: dict[str, dict[str, str]], admin: dict[str, str], setup: Farm, clock: Clock
) -> None:
    budi = abks["budi"]
    before = reminded(client, budi)
    assert before["2026-03-02"] == {"K1": ["Coryza", "Vitamin"], "K2": ["Coryza", "Vitamin"]}
    assert before["2026-03-08"] == {"K1": ["Coryza"], "K2": ["Coryza"]}

    task_key = key("VACCINATION", setup.coryza, setup.k1, "2026-03-02")
    response = record(client, budi, task_key, notes="  Semua ayam sudah  ")
    assert response.status_code == 201, response.text
    activity = response.json()["data"]
    assert activity == {
        "id": activity["id"],
        "status": "DONE",
        "skip_reason": None,
        "notes": "Semua ayam sudah",
        "activity_date": "2026-03-02",
        "recorded_at": activity["recorded_at"],
        "group": {"id": setup.k1, "code": "K1", "name": "Kandang 1", "location": "Blok A"},
        "age": {"week": 13, "day": 1},
        "type": "VACCINATION",
        "source_id": setup.coryza,
        "name": "Coryza",
        "target_disease": "Snot",
        "purpose": None,
        "method": "INTRAMUSCULAR",
        "dosage": "0,5 ml/ekor",
        "program_notes": "Suntik dada",
        "window": {"from": "2026-03-02", "to": "2026-03-08", "day": 1, "days": 7},
        "course": None,
        "reminder_task_key": task_key,
    }
    # The whole vaccination week of K1 is snoozed; K2 still has it, and the medicine is untouched.
    after = reminded(client, budi)
    assert after["2026-03-02"] == {"K1": ["Vitamin"], "K2": ["Coryza", "Vitamin"]}
    assert after["2026-03-08"] == {"K1": [], "K2": ["Coryza"]}
    listed = client.get(REMINDERS, headers=budi, params={"include_snoozed": True, "group_id": setup.k1}).json()
    coryza = listed["data"]["days"][3]["groups"][0]["tasks"][0]
    assert coryza["name"] == "Coryza" and coryza["snoozed_at"] is not None
    assert listed["data"]["days"][0]["groups"][0]["tasks"][1]["snoozed_at"] is None  # Vitamin
    staff = client.get("/api/v1/layer-health-reminders", headers=admin, params={"group_id": setup.k1}).json()
    assert [task["name"] for task in staff["data"]["days"][0]["groups"][0]["tasks"]] == ["Vitamin"]

    with SessionLocal() as db:
        snoozed = db.scalar(select(LayerHealthReminderSnooze))
        assert snoozed is not None
        assert (snoozed.occurrence_key, snoozed.source, snoozed.source_id, snoozed.snoozed_by_employee_id) == (
            f"VACCINATION-{setup.coryza}-{setup.k1}-2026-03-02",
            "LAYER_HEALTH_ACTIVITY",
            activity["id"],
            setup.budi,
        )
        event = db.scalar(select(AuditEvent).where(AuditEvent.entity_type == "layer_health_daily_activities"))
        assert event is not None
        assert (event.action, event.entity_id, event.reason) == (
            "layer_health_activity_recorded",
            activity["id"],
            "abk:budi",
        )

    # The snooze holds after the next nights' rebuilds.
    clock.today = TODAY + timedelta(days=1)
    with SessionLocal() as db:
        rebuild(db, clock.today)
    assert reminded(client, budi)["2026-03-08"] == {"K1": [], "K2": ["Coryza"]}


def test_activity_stands_alone_from_the_reminders(
    client: TestClient, abks: dict[str, dict[str, str]], admin: dict[str, str], setup: Farm, clock: Clock
) -> None:
    activity = record(client, abks["budi"], key("VACCINATION", setup.coryza, setup.k1, "2026-03-02")).json()["data"]
    # The program changes and the reminders are rebuilt (every row replaced): the activity keeps
    # what was reminded when it was recorded.
    body = {
        "age_week": 13,
        "vaccine_name": "Coryza",
        "target_disease": "Snot",
        "method": "SUBCUTANEOUS",
        "dosage": "1 ml/ekor",
        "notes": None,
        "is_active": True,
    }
    assert client.patch(f"/api/v1/layer-vaccinations/{setup.coryza}", headers=admin, json=body).status_code == 200
    with SessionLocal() as db:
        rebuild(db, TODAY)
    reminder = client.get(REMINDERS, headers=abks["budi"], params={"group_id": setup.k2}).json()["data"]
    assert reminder["days"][0]["groups"][0]["tasks"][0]["dosage"] == "1 ml/ekor"
    stored = client.get(f"{ACTIVITIES}/{activity['id']}", headers=abks["budi"]).json()["data"]
    assert stored == activity
    assert (stored["method"], stored["dosage"], stored["program_notes"]) == (
        "INTRAMUSCULAR",
        "0,5 ml/ekor",
        "Suntik dada",
    )


def test_medicine_course_days_are_recorded_one_by_one(
    client: TestClient, abks: dict[str, dict[str, str]], setup: Farm, clock: Clock
) -> None:
    budi = abks["budi"]
    first = record(client, budi, key("MEDICINE", setup.vitamin, setup.k1, "2026-03-02"))
    assert first.status_code == 201
    assert first.json()["data"]["course"] == {"day": 1, "days": 3} and first.json()["data"]["window"] is None
    # Only that day is snoozed: each course day is a dose of its own.
    assert reminded(client, budi, group_id=setup.k1)["2026-03-03"] == {"K1": ["Coryza", "Vitamin"]}
    assert reminded(client, budi, group_id=setup.k1)["2026-03-02"] == {"K1": ["Coryza"]}
    tomorrow = key("MEDICINE", setup.vitamin, setup.k1, "2026-03-03")
    early = record(client, budi, tomorrow)
    assert early.status_code == 422 and "task_key" in early.json()["errors"]
    clock.today = TODAY + timedelta(days=1)
    second = record(client, budi, tomorrow, notes="Sore")
    assert second.status_code == 201, second.text
    assert (second.json()["data"]["course"], second.json()["data"]["age"]) == (
        {"day": 2, "days": 3},
        {"week": 13, "day": 2},
    )


def test_skipped_needs_a_reason_and_is_stored_as_an_activity(
    client: TestClient, abks: dict[str, dict[str, str]], admin: dict[str, str], setup: Farm
) -> None:
    budi = abks["budi"]
    task_key = key("VACCINATION", setup.coryza, setup.k2, "2026-03-02")
    for body in ({"status": "SKIPPED"}, {"status": "SKIPPED", "skip_reason": "   "}, {"skip_reason": "Hujan"}):
        response = record(client, budi, task_key, **body)
        assert response.status_code == 422, response.text
    skipped = record(client, budi, task_key, status="SKIPPED", skip_reason="Ayam sedang sakit, tunggu dokter")
    assert skipped.status_code == 201, skipped.text
    assert (skipped.json()["data"]["status"], skipped.json()["data"]["skip_reason"]) == (
        "SKIPPED",
        "Ayam sedang sakit, tunggu dokter",
    )
    # A skipped occurrence is no longer reminded either.
    assert reminded(client, budi)["2026-03-04"]["K2"] == ["Vitamin"]
    listed = client.get(STAFF_ACTIVITIES, headers=admin, params={"status": "SKIPPED"}).json()
    assert [row["skip_reason"] for row in listed["data"]] == ["Ayam sedang sakit, tunggu dokter"]


def test_an_occurrence_is_recorded_once(
    client: TestClient, abks: dict[str, dict[str, str]], setup: Farm, clock: Clock
) -> None:
    assert record(client, abks["budi"], key("VACCINATION", setup.coryza, setup.k1, "2026-03-02")).status_code == 201
    again = record(client, abks["budi"], key("VACCINATION", setup.coryza, setup.k1, "2026-03-02"))
    assert again.status_code == 409 and again.json()["code"] == "LAYER_HEALTH_ACTIVITY_EXISTS"
    # Another day of the same vaccination week, by another ABK: still the same occurrence.
    clock.today = TODAY + timedelta(days=2)
    other_day = record(
        client,
        abks["citra"],
        key("VACCINATION", setup.coryza, setup.k1, "2026-03-04"),
        status="SKIPPED",
        skip_reason="x",
    )
    assert other_day.status_code == 409
    assert other_day.json() | {"detail": None} == {
        "code": "LAYER_HEALTH_ACTIVITY_EXISTS",
        "detail": None,
        "status": "DONE",
        "activity_date": "2026-03-02",
    }
    with SessionLocal() as db:
        assert len(db.scalars(select(LayerHealthDailyActivity)).all()) == 1


def test_recording_rules(
    client: TestClient, abks: dict[str, dict[str, str]], admin: dict[str, str], setup: Farm, clock: Clock
) -> None:
    budi = abks["budi"]
    coryza_k1 = key("VACCINATION", setup.coryza, setup.k1, "2026-03-02")
    assert client.post(ACTIVITIES, json={"task_key": coryza_k1, "status": "DONE"}).status_code == 401
    assert client.post(ACTIVITIES, headers=admin, json={"task_key": coryza_k1, "status": "DONE"}).status_code == 401
    forbidden = record(client, abks["dodi"], coryza_k1)
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "ABK_SCOPE_FORBIDDEN"
    assert record(client, budi, "VACCINATION-999-1-2026-03-02").status_code == 404
    future = record(client, budi, key("VACCINATION", setup.nd, setup.k1, "2026-03-09"))
    assert future.status_code == 422
    assert record(client, budi, coryza_k1, status="LATER").status_code == 422
    # A task of an earlier day can still be recorded (late), with that day as the activity date.
    clock.today = TODAY + timedelta(days=1)
    late = record(client, budi, key("MEDICINE", setup.vitamin, setup.k1, "2026-03-02"))
    assert late.status_code == 201 and late.json()["data"]["activity_date"] == "2026-03-02"
    # Not for a group closed since.
    closed = client.post(f"/api/v1/livestock-groups/{setup.k2}/close", headers=admin, json={"closed_at": "2026-03-03"})
    assert closed.status_code == 200
    refused = record(client, budi, key("VACCINATION", setup.coryza, setup.k2, "2026-03-03"))
    assert refused.status_code == 409 and refused.json()["code"] == "LAYER_HEALTH_GROUP_NOT_ACTIVE"


def test_reminders_snoozed_by_another_context(client: TestClient, abks: dict[str, dict[str, str]], setup: Farm) -> None:
    """The reminder context snoozes for any source; it does not depend on the activities."""
    occurrence = f"VACCINATION-{setup.coryza}-{setup.k1}-2026-03-02"
    with SessionLocal() as db:
        snooze(db, occurrence, source="SOMETHING_ELSE", source_id=None, employee_id=None)
        db.commit()
    assert reminded(client, abks["budi"])["2026-03-03"]["K1"] == ["Vitamin"]  # Coryza snoozed
    refused = record(client, abks["budi"], key("VACCINATION", setup.coryza, setup.k1, "2026-03-02"))
    assert refused.status_code == 409 and refused.json()["code"] == "LAYER_HEALTH_REMINDER_SNOOZED"


# --- Reading -----------------------------------------------------------------------------------


def test_abk_see_only_their_own_activities_and_staff_see_all(
    client: TestClient, abks: dict[str, dict[str, str]], admin: dict[str, str], setup: Farm
) -> None:
    budi, citra = abks["budi"], abks["citra"]
    mine = record(client, budi, key("VACCINATION", setup.coryza, setup.k1, "2026-03-02")).json()["data"]
    theirs = record(
        client, citra, key("MEDICINE", setup.vitamin, setup.k2, "2026-03-02"), status="SKIPPED", skip_reason="Habis"
    ).json()["data"]
    listed = client.get(ACTIVITIES, headers=budi).json()
    assert [row["id"] for row in listed["data"]] == [mine["id"]] and listed["meta"]["total"] == 1
    assert "recorded_by" not in listed["data"][0]
    assert client.get(f"{ACTIVITIES}/{theirs['id']}", headers=budi).status_code == 404
    assert client.get(ACTIVITIES, headers=abks["dodi"]).json()["data"] == []

    staff = client.get(STAFF_ACTIVITIES, headers=admin).json()
    assert staff["meta"]["total"] == 2
    by_id = {row["id"]: row for row in staff["data"]}
    assert by_id[mine["id"]]["recorded_by"] == {"employee_id": setup.budi, "name": "Budi"}
    assert by_id[theirs["id"]]["occurrence_key"] == f"MEDICINE-{setup.vitamin}-{setup.k2}-2026-03-02"
    detail = client.get(f"{STAFF_ACTIVITIES}/{mine['id']}", headers=admin).json()["data"]
    assert detail["recorded_by"]["name"] == "Budi"
    assert client.get(f"{STAFF_ACTIVITIES}/999", headers=admin).status_code == 404

    def ids(**params: Any) -> list[int]:
        return [row["id"] for row in client.get(STAFF_ACTIVITIES, headers=admin, params=params).json()["data"]]

    assert ids(type="VACCINATION") == [mine["id"]]
    assert ids(status="SKIPPED") == [theirs["id"]]
    assert ids(group_id=setup.k2) == [theirs["id"]]
    assert ids(employee_id=setup.budi) == [mine["id"]]
    assert ids(**{"from": "2026-03-03"}) == []
    assert sorted(ids(**{"from": "2026-03-02", "to": "2026-03-02"})) == sorted([mine["id"], theirs["id"]])
    assert client.get(STAFF_ACTIVITIES, headers=admin, params={"status": "LATER"}).status_code == 422
    assert client.get(STAFF_ACTIVITIES, headers=budi).status_code == 401
