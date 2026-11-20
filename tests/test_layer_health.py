"""Layer health program setup: vaccination schedule and medicine program (Admin/Superadmin)."""

from collections.abc import Iterator
from datetime import date
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import LAYER_HEALTH_METHODS, AuditEvent, Employee, User
from app.db.session import SessionLocal
from app.features.layer_health import Method
from app.main import app

PASSWORD = "StrongPassword123"
VACCINATIONS = "/api/v1/layer-vaccinations"
MEDICINES = "/api/v1/layer-medicine-programs"


@pytest.fixture
def client(clean_database: None) -> Iterator[TestClient]:
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
        db.commit()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def vaccination(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "age_week": 4,
        "vaccine_name": "ND-IB Live",
        "target_disease": "ND, IB",
        "method": "EYE_DROP",
        "dosage": "1 tetes/ekor",
        "notes": "Air minum tanpa klorin",
    }
    return body | changes


def medicine(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "medicine_name": "Vita Stress",
        "purpose": "Vitamin",
        "method": "DRINKING_WATER",
        "dosage": "1 g / 2 L air minum",
        "start_age_week": 14,
        "start_day": 1,
        "course_days": 3,
        "interval_value": 2,
        "interval_unit": "WEEK",
        "notes": None,
    }
    return body | changes


def test_method_list_matches_database_constraint() -> None:
    assert get_args(Method) == LAYER_HEALTH_METHODS


# --- Vaccination schedule ----------------------------------------------------------------------


def test_vaccination_create_read_update_and_audit(client: TestClient, admin: dict[str, str]) -> None:
    created = client.post(VACCINATIONS, headers=admin, json=vaccination(vaccine_name="  ND-IB Live  "))
    assert created.status_code == 201, created.text
    row = created.json()["data"]
    assert {key: row[key] for key in vaccination()} == vaccination()  # the name is trimmed
    assert row["is_active"] is True
    assert client.get(f"{VACCINATIONS}/{row['id']}", headers=admin).json()["data"] == row

    update = vaccination(age_week=5, dosage="", notes=None, is_active=True)
    updated = client.patch(f"{VACCINATIONS}/{row['id']}", headers=admin, json=update)
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["age_week"] == 5
    assert updated.json()["data"]["dosage"] is None  # blank text is stored as "not set"
    assert updated.json()["data"]["notes"] is None

    with SessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "layer_vaccinations").order_by(AuditEvent.id)
        ).all()
    assert [event.action for event in events] == ["layer_vaccination_created", "layer_vaccination_updated"]
    assert events[1].before is not None and events[1].before["age_week"] == 4
    assert events[1].after is not None and events[1].after["age_week"] == 5


def test_vaccination_schedule_is_ordered_by_age_and_filterable(client: TestClient, admin: dict[str, str]) -> None:
    for body in (
        vaccination(age_week=18, vaccine_name="ND-IB-EDS Killed", method="INTRAMUSCULAR", target_disease="ND, IB, EDS"),
        vaccination(age_week=1, vaccine_name="ND-IB Live"),
        vaccination(age_week=3, vaccine_name="Gumboro", method="DRINKING_WATER", target_disease="IBD"),
        vaccination(age_week=1, vaccine_name="AI Killed", method="SUBCUTANEOUS", target_disease="AI"),
    ):
        assert client.post(VACCINATIONS, headers=admin, json=body).status_code == 201
    listed = client.get(VACCINATIONS, headers=admin).json()["data"]
    assert [(row["age_week"], row["vaccine_name"]) for row in listed] == [
        (1, "AI Killed"),
        (1, "ND-IB Live"),
        (3, "Gumboro"),
        (18, "ND-IB-EDS Killed"),
    ]
    gumboro = listed[2]
    client.patch(
        f"{VACCINATIONS}/{gumboro['id']}",
        headers=admin,
        json={key: gumboro[key] for key in vaccination()} | {"is_active": False},
    )
    active = client.get(VACCINATIONS, headers=admin, params={"is_active": True}).json()["data"]
    assert "Gumboro" not in [row["vaccine_name"] for row in active]
    inactive = client.get(VACCINATIONS, headers=admin, params={"is_active": False}).json()["data"]
    assert [row["vaccine_name"] for row in inactive] == ["Gumboro"]
    at_week_1 = client.get(VACCINATIONS, headers=admin, params={"age_week": 1}).json()["data"]
    assert len(at_week_1) == 2
    by_disease = client.get(VACCINATIONS, headers=admin, params={"q": "eds"}).json()["data"]
    assert [row["vaccine_name"] for row in by_disease] == ["ND-IB-EDS Killed"]


def test_same_vaccine_once_per_age(client: TestClient, admin: dict[str, str]) -> None:
    first = client.post(VACCINATIONS, headers=admin, json=vaccination()).json()["data"]
    duplicate = client.post(VACCINATIONS, headers=admin, json=vaccination(vaccine_name="nd-ib live"))
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "LAYER_VACCINATION_EXISTS"
    assert duplicate.json()["existing_id"] == first["id"]
    # The same vaccine at another age is a booster, not a duplicate.
    booster = client.post(VACCINATIONS, headers=admin, json=vaccination(age_week=8)).json()["data"]
    moved = client.patch(f"{VACCINATIONS}/{booster['id']}", headers=admin, json=vaccination(is_active=True))
    assert moved.status_code == 409 and moved.json()["code"] == "LAYER_VACCINATION_EXISTS"
    # Re-saving an item unchanged is not a conflict with itself; an inactive item still blocks.
    deactivated = client.patch(f"{VACCINATIONS}/{first['id']}", headers=admin, json=vaccination(is_active=False))
    assert deactivated.status_code == 200
    assert client.post(VACCINATIONS, headers=admin, json=vaccination()).status_code == 409


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"age_week": -1}, "age_week"),
        ({"age_week": 201}, "age_week"),
        ({"vaccine_name": "   "}, "vaccine_name"),
        ({"vaccine_name": "x" * 256}, "vaccine_name"),
        ({"method": "INJECTION"}, "method"),
        ({"method": None}, "method"),
        ({"unexpected": 1}, "unexpected"),
    ],
)
def test_vaccination_validation(
    client: TestClient, admin: dict[str, str], change: dict[str, object], field: str
) -> None:
    response = client.post(VACCINATIONS, headers=admin, json=vaccination(**change))
    assert response.status_code == 422, response.text
    assert field in response.json()["errors"], response.json()


def test_vaccination_week_zero_and_update_requires_full_item(client: TestClient, admin: dict[str, str]) -> None:
    row = client.post(VACCINATIONS, headers=admin, json=vaccination(age_week=0)).json()["data"]
    assert row["age_week"] == 0
    partial = client.patch(f"{VACCINATIONS}/{row['id']}", headers=admin, json={"is_active": False})
    assert partial.status_code == 422
    assert client.get(f"{VACCINATIONS}/999", headers=admin).status_code == 404
    assert client.patch(f"{VACCINATIONS}/999", headers=admin, json=vaccination(is_active=True)).status_code == 404


# --- Medicine program --------------------------------------------------------------------------


def test_medicine_create_read_update_and_audit(client: TestClient, admin: dict[str, str]) -> None:
    created = client.post(MEDICINES, headers=admin, json=medicine())
    assert created.status_code == 201, created.text
    row = created.json()["data"]
    assert {key: row[key] for key in medicine()} == medicine()
    assert row["is_active"] is True
    assert client.get(f"{MEDICINES}/{row['id']}", headers=admin).json()["data"] == row

    # First course on day 4 of age week 18, 5 days long, then every month.
    update = medicine(
        interval_value=1, interval_unit="MONTH", start_age_week=18, start_day=4, course_days=5, is_active=True
    )
    updated = client.patch(f"{MEDICINES}/{row['id']}", headers=admin, json=update)
    assert updated.status_code == 200, updated.text
    data = updated.json()["data"]
    assert {key: data[key] for key in update} == update
    # start_day and course_days default to 1.
    single = client.patch(
        f"{MEDICINES}/{row['id']}", headers=admin, json=update | {"start_day": None, "course_days": None}
    )
    assert single.status_code == 422
    defaults = {key: value for key, value in update.items() if key not in ("start_day", "course_days")}
    single = client.patch(f"{MEDICINES}/{row['id']}", headers=admin, json=defaults)
    assert (single.json()["data"]["start_day"], single.json()["data"]["course_days"]) == (1, 1)

    with SessionLocal() as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_type == "layer_medicine_programs").order_by(AuditEvent.id)
        ).all()
    assert actions == [
        "layer_medicine_program_created",
        "layer_medicine_program_updated",
        "layer_medicine_program_updated",
    ]  # the rejected update left no trace


def test_medicine_programs_order_and_filters(client: TestClient, admin: dict[str, str]) -> None:
    for body in (
        medicine(
            medicine_name="Obat Cacing",
            purpose="Cacingan",
            interval_value=3,
            interval_unit="MONTH",
            start_age_week=8,
            course_days=1,
            method="FEED",
        ),
        medicine(medicine_name="Vita Stress", start_age_week=2, start_day=5),
        medicine(
            medicine_name="Antikoksi", purpose="Koksidiosis", interval_value=10, interval_unit="DAY", start_age_week=2
        ),
        medicine(medicine_name="Elektrolit", interval_value=1, interval_unit="WEEK", start_age_week=0),
    ):
        assert client.post(MEDICINES, headers=admin, json=body).status_code == 201, body
    listed = client.get(MEDICINES, headers=admin).json()["data"]
    # By the age (week, then day) of the first course.
    assert [row["medicine_name"] for row in listed] == ["Elektrolit", "Antikoksi", "Vita Stress", "Obat Cacing"]
    by_purpose = client.get(MEDICINES, headers=admin, params={"q": "cacing"}).json()["data"]
    assert [row["medicine_name"] for row in by_purpose] == ["Obat Cacing"]
    electrolyte = listed[0]
    client.patch(
        f"{MEDICINES}/{electrolyte['id']}",
        headers=admin,
        json={key: electrolyte[key] for key in medicine()} | {"is_active": False},
    )
    active = client.get(MEDICINES, headers=admin, params={"is_active": True}).json()["data"]
    assert [row["medicine_name"] for row in active] == ["Antikoksi", "Vita Stress", "Obat Cacing"]


def test_one_program_per_medicine(client: TestClient, admin: dict[str, str]) -> None:
    first = client.post(MEDICINES, headers=admin, json=medicine()).json()["data"]
    duplicate = client.post(MEDICINES, headers=admin, json=medicine(medicine_name=" vita STRESS "))
    assert duplicate.status_code == 409
    assert duplicate.json() | {"detail": None} == {
        "code": "LAYER_MEDICINE_EXISTS",
        "detail": None,
        "existing_id": first["id"],
    }
    other = client.post(MEDICINES, headers=admin, json=medicine(medicine_name="Vita Chick")).json()["data"]
    renamed = client.patch(f"{MEDICINES}/{other['id']}", headers=admin, json=medicine(is_active=True))
    assert renamed.status_code == 409
    assert client.patch(f"{MEDICINES}/{first['id']}", headers=admin, json=medicine(is_active=True)).status_code == 200


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"interval_value": 0}, "interval_value"),
        ({"interval_value": None}, "interval_value"),
        ({"interval_unit": "YEAR"}, "interval_unit"),
        ({"interval_value": 366, "interval_unit": "DAY"}, "request"),
        ({"interval_value": 53, "interval_unit": "WEEK"}, "request"),
        ({"interval_value": 13, "interval_unit": "MONTH"}, "request"),
        ({"start_age_week": -1}, "start_age_week"),
        ({"start_age_week": 201}, "start_age_week"),
        ({"start_age_week": None}, "start_age_week"),  # every medicine says when it starts
        ({"start_day": 0}, "start_day"),
        ({"start_day": 8}, "start_day"),
        ({"course_days": 0}, "course_days"),
        ({"course_days": 31}, "course_days"),
        # A course cannot run into the next one.
        ({"course_days": 3, "interval_value": 2, "interval_unit": "DAY"}, "request"),
        ({"course_days": 15, "interval_value": 2, "interval_unit": "WEEK"}, "request"),
        ({"medicine_name": ""}, "medicine_name"),
        ({"method": "INJECTION"}, "method"),
    ],
)
def test_medicine_validation(client: TestClient, admin: dict[str, str], change: dict[str, object], field: str) -> None:
    response = client.post(MEDICINES, headers=admin, json=medicine(**change))
    assert response.status_code == 422, response.text
    assert field in response.json()["errors"], response.json()


def test_medicine_interval_limits_are_inclusive(client: TestClient, admin: dict[str, str]) -> None:
    cases = ((365, "DAY", 30), (52, "WEEK", 30), (12, "MONTH", 30), (1, "DAY", 1), (2, "WEEK", 14), (1, "MONTH", 28))
    for index, (value, unit, course_days) in enumerate(cases):
        body = medicine(
            medicine_name=f"Obat {index}", interval_value=value, interval_unit=unit, course_days=course_days
        )
        assert client.post(MEDICINES, headers=admin, json=body).status_code == 201


def test_medicine_not_found(client: TestClient, admin: dict[str, str]) -> None:
    assert client.get(f"{MEDICINES}/999", headers=admin).status_code == 404
    assert client.patch(f"{MEDICINES}/999", headers=admin, json=medicine(is_active=True)).status_code == 404


# --- Access ------------------------------------------------------------------------------------


def test_setup_requires_staff_login(client: TestClient, admin: dict[str, str]) -> None:
    for url in (VACCINATIONS, MEDICINES):
        assert client.get(url).status_code == 401
        assert client.post(url, json={}).status_code == 401
    # ABK credentials do not open staff endpoints.
    with SessionLocal() as db:
        employee = Employee(name="Budi", job_title="ABK", joined_at=date(2025, 1, 1), status="ACTIVE")
        db.add(employee)
        db.commit()
        employee_id = employee.id
    pin = client.post(
        "/api/v1/abk-accounts", headers=admin, json={"employee_id": employee_id, "username": "budi", "scopes": ["AYAM"]}
    ).json()["data"]["pin"]
    abk = {"X-ABK-Username": "budi", "X-ABK-PIN": pin}
    assert client.get(VACCINATIONS, headers=abk).status_code == 401
    assert client.get(MEDICINES, headers=abk).status_code == 401
