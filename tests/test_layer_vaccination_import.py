"""Layer vaccination schedule from Excel: template download and import (replaces the schedule)."""

import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import AuditEvent, Employee, LayerVaccination, LivestockGroup, User
from app.db.session import SessionLocal
from app.features import abk, layer_health, layer_health_activities, layer_health_reminders
from app.features.layer_health_reminders import rebuild
from app.features.layer_vaccination_excel import COLUMNS
from app.main import app

PASSWORD = "StrongPassword123"
TODAY = date(2026, 3, 2)
TEMPLATE = "/api/v1/layer-vaccinations/template"
IMPORT = "/api/v1/layer-vaccinations/import"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(frozen=True)
class Schedule:
    nd: int  # week 4, active
    coryza: int  # week 13, active
    eds: int  # week 18, active
    gumboro: int  # week 10, inactive


@pytest.fixture
def schedule(clean_database: None, monkeypatch: pytest.MonkeyPatch) -> Schedule:
    for module in (abk, layer_health, layer_health_reminders, layer_health_activities):
        monkeypatch.setattr(module, "today_local", lambda: TODAY)
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
        rows = [
            LayerVaccination(
                age_week=4,
                vaccine_name="ND-IB Live",
                target_disease="ND, IB",
                method="EYE_DROP",
                dosage="1 tetes/ekor",
                is_active=True,
            ),
            LayerVaccination(
                age_week=13, vaccine_name="Coryza", method="INTRAMUSCULAR", dosage="0,5 ml/ekor", is_active=True
            ),
            LayerVaccination(age_week=18, vaccine_name="EDS", method="INTRAMUSCULAR", is_active=True),
            LayerVaccination(age_week=10, vaccine_name="Gumboro", method="DRINKING_WATER", is_active=False),
        ]
        db.add_all(rows)
        db.commit()
        return Schedule(*(row.id for row in rows))


@pytest.fixture
def client(schedule: Schedule) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def xlsx(rows: Sequence[Sequence[Any]], header: Sequence[str] = COLUMNS, sheet: str = "Vaccinations") -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = sheet
    worksheet.append(list(header))
    for row in rows:
        worksheet.append(list(row))
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def upload(
    client: TestClient, headers: dict[str, str], content: bytes, *, dry_run: bool = False, name: str = "jadwal.xlsx"
) -> Any:
    return client.post(IMPORT, headers=headers, params={"dry_run": dry_run}, files={"file": (name, content, XLSX)})


def active_schedule() -> list[tuple[int, str, str, str | None]]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(LayerVaccination)
            .where(LayerVaccination.is_active.is_(True))
            .order_by(LayerVaccination.age_week, LayerVaccination.vaccine_name)
        ).all()
        return [(row.age_week, row.vaccine_name, row.method, row.dosage) for row in rows]


def import_events() -> list[tuple[str, int, str | None]]:
    with SessionLocal() as db:
        events = db.scalars(
            select(AuditEvent).where(AuditEvent.entity_type == "layer_vaccinations").order_by(AuditEvent.id)
        ).all()
        return [(event.action, event.entity_id, event.reason) for event in events]


# --- Template ----------------------------------------------------------------------------------


def test_template_is_prefilled_with_the_active_schedule(client: TestClient, admin: dict[str, str]) -> None:
    response = client.get(TEMPLATE, headers=admin)
    assert response.status_code == 200
    assert response.headers["content-type"] == XLSX
    assert response.headers["content-disposition"] == 'attachment; filename="layer-vaccination-schedule.xlsx"'
    workbook = load_workbook(io.BytesIO(response.content))
    assert workbook.sheetnames == ["Vaccinations", "Instructions"]
    sheet = workbook["Vaccinations"]
    assert list(sheet.iter_rows(values_only=True)) == [
        COLUMNS,
        (4, "ND-IB Live", "ND, IB", "EYE_DROP", "1 tetes/ekor", None),
        (13, "Coryza", None, "INTRAMUSCULAR", "0,5 ml/ekor", None),
        (18, "EDS", None, "INTRAMUSCULAR", None, None),
    ]  # the inactive Gumboro is not part of the schedule
    checks = {str(rule.sqref): rule.type for rule in sheet.data_validations.dataValidation}
    assert checks == {"A2:A501": "whole", "D2:D501": "list"}

    empty = client.get(TEMPLATE, headers=admin, params={"include_current": False})
    assert list(load_workbook(io.BytesIO(empty.content))["Vaccinations"].iter_rows(values_only=True)) == [COLUMNS]
    assert client.get(TEMPLATE).status_code == 401


def test_the_template_imports_back_unchanged(client: TestClient, admin: dict[str, str]) -> None:
    template = client.get(TEMPLATE, headers=admin).content
    response = upload(client, admin, template)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["summary"] == {
        "rows": 3,
        "created": 0,
        "updated": 0,
        "unchanged": 3,
        "deactivated": 0,
    }
    assert import_events() == []  # nothing changed, nothing to audit


# --- Import ------------------------------------------------------------------------------------


def replacement() -> bytes:
    return xlsx(
        [
            (4, "nd-ib live", "ND, IB", "EYE_DROP", "2 tetes/ekor", None),  # existing: new dosage (and casing)
            (13, "Coryza", None, "INTRAMUSCULAR", "0,5 ml/ekor", None),  # existing, identical
            (10, "Gumboro", "IBD", "DRINKING_WATER", None, None),  # existing but inactive: back in the schedule
            (20, "AI Killed", "AI", "SUBCUTANEOUS", "0,5 ml/ekor", "Suntik leher"),  # new
        ]
    )  # EDS (week 18) is not in the file


def test_import_replaces_the_whole_schedule(client: TestClient, admin: dict[str, str], schedule: Schedule) -> None:
    response = upload(client, admin, replacement())
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is False
    assert data["summary"] == {"rows": 4, "created": 1, "updated": 2, "unchanged": 1, "deactivated": 1}
    assert [(row["id"], row["age_week"], row["vaccine_name"], row["change"]) for row in data["schedule"]] == [
        (schedule.nd, 4, "nd-ib live", "UPDATED"),
        (schedule.gumboro, 10, "Gumboro", "UPDATED"),
        (schedule.coryza, 13, "Coryza", "UNCHANGED"),
        (data["schedule"][3]["id"], 20, "AI Killed", "CREATED"),
    ]
    assert data["schedule"][3]["notes"] == "Suntik leher"
    assert data["deactivated"] == [{"id": schedule.eds, "age_week": 18, "vaccine_name": "EDS"}]
    # The file is now the schedule; the vaccinations not in it are no longer valid (kept, inactive).
    assert active_schedule() == [
        (4, "nd-ib live", "EYE_DROP", "2 tetes/ekor"),
        (10, "Gumboro", "DRINKING_WATER", None),
        (13, "Coryza", "INTRAMUSCULAR", "0,5 ml/ekor"),
        (20, "AI Killed", "SUBCUTANEOUS", "0,5 ml/ekor"),
    ]
    listed = client.get("/api/v1/layer-vaccinations", headers=admin, params={"is_active": False}).json()["data"]
    assert [row["vaccine_name"] for row in listed] == ["EDS"]
    created_id = data["schedule"][3]["id"]
    assert sorted(import_events()) == sorted(
        [
            ("layer_vaccination_import_updated", schedule.nd, "import:jadwal.xlsx"),
            ("layer_vaccination_import_updated", schedule.gumboro, "import:jadwal.xlsx"),
            ("layer_vaccination_imported", created_id, "import:jadwal.xlsx"),
            ("layer_vaccination_import_deactivated", schedule.eds, "import:jadwal.xlsx"),
        ]
    )


def test_dry_run_changes_nothing(client: TestClient, admin: dict[str, str], schedule: Schedule) -> None:
    before = active_schedule()
    response = upload(client, admin, replacement(), dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["dry_run"] is True
    assert data["summary"] == {"rows": 4, "created": 1, "updated": 2, "unchanged": 1, "deactivated": 1}
    # Nothing is created, so a new row has no id yet.
    assert (data["schedule"][3]["vaccine_name"], data["schedule"][3]["id"]) == ("AI Killed", None)
    assert active_schedule() == before and import_events() == []


def test_invalid_rows_reject_the_whole_file(client: TestClient, admin: dict[str, str]) -> None:
    before = active_schedule()
    content = xlsx(
        [
            (-1, "A", None, "ORAL", None, None),  # row 2
            (5, None, None, "ORAL", None, None),  # row 3
            (6, "B", None, "INJECTION", None, None),  # row 4
            (4.5, "C", None, "ORAL", None, None),  # row 5
            (7, "D", None, "ORAL", None, None),  # row 6: valid
            (7, "d", None, "ORAL", None, None),  # row 7: same vaccine and week as row 6
            (True, "E", None, "ORAL", None, None),  # row 8
            (8, "F" * 256, None, "ORAL", None, None),  # row 9
        ]
    )
    response = upload(client, admin, content)
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert set(errors) == {
        "rows.2.age_week",
        "rows.3.vaccine_name",
        "rows.4.method",
        "rows.5.age_week",
        "rows.7.vaccine_name",
        "rows.8.age_week",
        "rows.9.vaccine_name",
    }
    assert errors["rows.7.vaccine_name"] == ["The same vaccine and age week as row 6."]
    assert active_schedule() == before


def test_cells_are_read_like_a_person_types_them(client: TestClient, admin: dict[str, str]) -> None:
    content = xlsx(
        [
            ("Eye drop", "4", "ND Live"),
            (None, None, None),  # blank rows are skipped
            ("drinking-water", 10.0, "Gumboro", 1),  # dosage typed as a number
            ("  oral ", 12, "  Spaced  "),
        ],
        header=["Method", "AGE_WEEK", "vaccine name", "dosage"],  # any order, optional columns left out
        sheet="Sheet1",
    )
    response = upload(client, admin, content)
    assert response.status_code == 200, response.text
    assert active_schedule() == [
        (4, "ND Live", "EYE_DROP", None),
        (10, "Gumboro", "DRINKING_WATER", "1"),
        (12, "Spaced", "ORAL", None),
    ]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"not an excel file", "Upload an Excel .xlsx file"),
        (xlsx([(4, "A", "ORAL")], header=["age_week", "vaccine_name", "method", "dosis"]), "Unknown columns: dosis"),
        (xlsx([(4, "A")], header=["age_week", "vaccine_name"]), "Missing columns: method"),
        (xlsx([(4, "A", "ORAL", 4)], header=["age_week", "vaccine_name", "method", "age_week"]), "appears twice"),
        (xlsx([]), "no vaccination"),
        (xlsx([(4, "A", None, "ORAL", None, None, "extra")]), "outside the template's columns"),
        (xlsx([(week % 201, f"V{week}", None, "ORAL", None, None) for week in range(501)]), "At most 500"),
    ],
)
def test_file_problems(client: TestClient, admin: dict[str, str], content: bytes, message: str) -> None:
    response = upload(client, admin, content)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "INVALID_FILE"
    assert message in response.json()["errors"]["file"][0]


def test_file_size_and_access(client: TestClient, admin: dict[str, str], schedule: Schedule) -> None:
    too_large = upload(client, admin, b"0" * (2 * 1024 * 1024 + 1))
    assert too_large.status_code == 413 and too_large.json()["code"] == "FILE_TOO_LARGE"
    assert client.post(IMPORT, files={"file": ("a.xlsx", replacement(), XLSX)}).status_code == 401
    assert client.post(IMPORT, headers=admin).status_code == 422  # no file


def test_what_the_abk_recorded_survives_an_import(
    client: TestClient, admin: dict[str, str], schedule: Schedule
) -> None:
    """Matching vaccinations keep their id, so this week's recorded vaccination stays snoozed and its
    activity keeps what was reminded; a vaccination left out of the file stops being reminded."""
    with SessionLocal() as db:
        group = LivestockGroup(
            species="AYAM",
            code="K1",
            name="Kandang 1",
            start_date=TODAY,
            initial_population=100,
            arrival_age_week=13,
            location="Blok A",
            status="ACTIVE",
        )
        employee = Employee(name="Budi", job_title="ABK", joined_at=date(2025, 1, 1), status="ACTIVE")
        db.add_all([group, employee])
        db.commit()
        group_id, employee_id = group.id, employee.id
        rebuild(db, TODAY)
    pin = client.post(
        "/api/v1/abk-accounts", headers=admin, json={"employee_id": employee_id, "username": "budi", "scopes": ["AYAM"]}
    ).json()["data"]["pin"]
    budi = {"X-ABK-Username": "budi", "X-ABK-PIN": pin}
    task_key = f"VACCINATION-{schedule.coryza}-{group_id}-2026-03-02"
    recorded = client.post(
        "/api/v1/abk/layer-health-activities", headers=budi, json={"task_key": task_key, "status": "DONE"}
    )
    assert recorded.status_code == 201, recorded.text

    # Coryza stays (new dosage); a new week-13 vaccination is added.
    content = xlsx(
        [
            (13, "Coryza", None, "INTRAMUSCULAR", "1 ml/ekor", None),
            (13, "AI Killed", None, "SUBCUTANEOUS", None, None),
        ]
    )
    assert upload(client, admin, content).json()["data"]["summary"]["deactivated"] == 2  # ND-IB Live and EDS
    with SessionLocal() as db:
        rebuild(db, TODAY)
    reminders = client.get("/api/v1/abk/layer-health-reminders", headers=budi, params={"days": 0}).json()["data"]
    assert [task["name"] for task in reminders["days"][0]["groups"][0]["tasks"]] == ["AI Killed"]
    activity = client.get(f"/api/v1/abk/layer-health-activities/{recorded.json()['data']['id']}", headers=budi)
    assert activity.json()["data"]["dosage"] == "0,5 ml/ekor"  # as it was reminded
