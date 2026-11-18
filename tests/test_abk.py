"""ABK Web App: PIN authentication, draft daily reports, and Admin/Superadmin review."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.security import hash_password
from app.db.models import (
    AbkAccount,
    AbkDailyReport,
    AuditEvent,
    DailyRecord,
    Employee,
    FeedType,
    InventoryOpeningBalance,
    LeleBiomassSnapshot,
    LivestockGroup,
    User,
)
from app.db.session import SessionLocal
from app.features.farm import feed_balance
from app.main import app

PASSWORD = "StrongPassword123"


@dataclass(frozen=True)
class Farm:
    ayam_group: int
    lele_group: int
    closed_group: int
    ayam_feed: int
    lele_feed: int
    inactive_feed: int
    employee: int
    inactive_employee: int


@pytest.fixture
def farm(clean_database: None) -> Farm:
    with SessionLocal() as db:
        admin = User(
            name="Admin",
            email="admin@example.com",
            password_hash=hash_password(PASSWORD),
            role="ADMIN",
            is_active=True,
            must_change_password=False,
        )
        ayam_feed = FeedType(code="AYAM-BR-1", species="AYAM", name="BR 1", is_active=True)
        lele_feed = FeedType(code="LELE-781", species="LELE", name="781", is_active=True)
        inactive_feed = FeedType(code="AYAM-OLD", species="AYAM", name="Old", is_active=False)
        groups = [
            LivestockGroup(
                species="AYAM",
                code="KANDANG-1",
                name="Kandang 1",
                start_date=date(2026, 1, 1),
                initial_population=100,
                location="Blok A",
                status="ACTIVE",
            ),
            LivestockGroup(
                species="LELE",
                code="KOLAM-1",
                name="Kolam 1",
                start_date=date(2026, 1, 1),
                initial_population=1000,
                initial_weight_g=Decimal("10"),
                location="Blok B",
                status="ACTIVE",
            ),
            LivestockGroup(
                species="AYAM",
                code="KANDANG-OLD",
                name="Kandang Lama",
                start_date=date(2025, 1, 1),
                initial_population=10,
                location="Blok C",
                status="CLOSED",
                closed_at=date(2025, 12, 31),
            ),
        ]
        employee = Employee(name="Budi", job_title="ABK", joined_at=date(2025, 1, 1), status="ACTIVE")
        inactive_employee = Employee(name="Andi", job_title="ABK", joined_at=date(2025, 1, 1), status="INACTIVE")
        db.add_all([admin, ayam_feed, lele_feed, inactive_feed, *groups, employee, inactive_employee])
        db.flush()
        db.add(
            InventoryOpeningBalance(
                item_kind="FEED",
                feed_type_id=ayam_feed.id,
                quantity_kg=Decimal("100"),
                effective_date=date(2026, 1, 1),
                created_by=admin.id,
            )
        )
        db.commit()
        return Farm(
            groups[0].id,
            groups[1].id,
            groups[2].id,
            ayam_feed.id,
            lele_feed.id,
            inactive_feed.id,
            employee.id,
            inactive_employee.id,
        )


@pytest.fixture
def client(farm: Farm) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
def abk(client: TestClient, admin: dict[str, str], farm: Farm) -> dict[str, str]:
    """Headers of a freshly issued ABK account (username "budi")."""
    response = client.post(
        "/api/v1/abk-accounts",
        headers=admin,
        json={"employee_id": farm.employee, "username": " Budi ", "scopes": ["AYAM", "LELE"]},
    )
    assert response.status_code == 201, response.text
    return {"X-ABK-Username": "budi", "X-ABK-PIN": response.json()["data"]["pin"]}


def ayam_report(farm: Farm, **changes: object) -> dict[str, object]:
    report: dict[str, object] = {
        "group_id": farm.ayam_group,
        "record_date": "2026-02-01",
        "mortality": 2,
        "notes": "Hujan deras",
        "feeds": [{"feed_type_id": farm.ayam_feed, "morning_kg": "3.5", "noon_kg": "2.25"}],
        "eggs": {
            "UTUH": {"weight_kg": "5", "quantity_pieces": 80},
            "PECAH": {"weight_kg": "0.5", "quantity_pieces": 8},
            "REJECT": {"weight_kg": "0", "quantity_pieces": 0},
        },
    }
    return report | changes


def lele_report(farm: Farm, **changes: object) -> dict[str, object]:
    report: dict[str, object] = {
        "group_id": farm.lele_group,
        "record_date": "2026-02-01",
        "mortality": 15,
        "feeds": [{"feed_type_id": farm.lele_feed, "morning_kg": "1", "noon_kg": "1", "evening_kg": "1.5"}],
    }
    return report | changes


def daily_record_count() -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(DailyRecord)) or 0


# --- Accounts ---------------------------------------------------------------------------------


def test_account_pin_is_shown_once_and_never_stored_in_clear(
    client: TestClient, admin: dict[str, str], abk: dict[str, str]
) -> None:
    pin = abk["X-ABK-PIN"]
    assert len(pin) == 6 and pin.isdigit()
    listed = client.get("/api/v1/abk-accounts", headers=admin).json()
    assert listed["meta"]["total"] == 1
    account = listed["data"][0]
    assert account["username"] == "budi"
    assert account["employee"]["name"] == "Budi"
    assert "pin" not in account and "pin_hash" not in account
    with SessionLocal() as db:
        stored = db.scalar(select(AbkAccount))
        assert stored is not None and pin not in stored.pin_hash
        audits = db.scalars(select(AuditEvent).where(AuditEvent.entity_type == "abk_accounts")).all()
        assert audits and all("pin_hash" not in (row.after or {}) for row in audits)


def test_account_rules(client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm) -> None:
    url = "/api/v1/abk-accounts"
    same_employee = client.post(
        url, headers=admin, json={"employee_id": farm.employee, "username": "budi2", "scopes": ["AYAM"]}
    )
    assert same_employee.json()["code"] == "ABK_ACCOUNT_EXISTS"
    inactive = client.post(
        url, headers=admin, json={"employee_id": farm.inactive_employee, "username": "andi", "scopes": ["AYAM"]}
    )
    assert inactive.status_code == 422 and "employee_id" in inactive.json()["errors"]
    with SessionLocal() as db:
        other = Employee(name="Citra", joined_at=date(2025, 1, 1), status="ACTIVE")
        db.add(other)
        db.commit()
        other_id = other.id
    taken = client.post(url, headers=admin, json={"employee_id": other_id, "username": "BUDI", "scopes": ["AYAM"]})
    assert taken.status_code == 409 and taken.json()["code"] == "ABK_USERNAME_EXISTS"
    invalid = client.post(
        url, headers=admin, json={"employee_id": other_id, "username": "no spaces", "scopes": ["AYAM"]}
    )
    assert invalid.status_code == 422
    assert client.post(url, json={"employee_id": other_id, "username": "citra", "scopes": ["AYAM"]}).status_code == 401


# --- Authentication ---------------------------------------------------------------------------


def test_abk_credentials_are_required_on_every_request(client: TestClient, abk: dict[str, str]) -> None:
    url = "/api/v1/abk/form-options"
    assert client.get(url).status_code == 401
    assert client.get(url, headers={"X-ABK-Username": "budi"}).status_code == 401
    assert client.get(url, headers=abk | {"X-ABK-Username": "nobody"}).json()["code"] == "INVALID_ABK_CREDENTIALS"
    wrong_pin = "000000" if abk["X-ABK-PIN"] != "000000" else "111111"
    assert client.get(url, headers=abk | {"X-ABK-PIN": wrong_pin}).status_code == 401
    ok = client.get(url, headers=abk | {"X-ABK-Username": "BUDI"})  # usernames are case-insensitive
    assert ok.status_code == 200
    assert ok.json()["data"]["employee"]["username"] == "budi"


def test_abk_and_staff_credentials_do_not_mix(client: TestClient, admin: dict[str, str], abk: dict[str, str]) -> None:
    # An ABK cannot use any staff endpoint, and a staff JWT does not open the ABK endpoints.
    assert client.get("/api/v1/daily-records?species=AYAM", headers=abk).status_code == 401
    assert client.get("/api/v1/abk-daily-reports", headers=abk).status_code == 401
    assert client.get("/api/v1/abk/form-options", headers=admin).status_code == 401


def test_inactive_account_or_employee_cannot_submit(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    with SessionLocal() as db:
        account_id = db.scalar(select(AbkAccount.id))
    url = "/api/v1/abk/form-options"
    client.patch(f"/api/v1/abk-accounts/{account_id}", headers=admin, json={"username": "budi", "is_active": False})
    assert client.get(url, headers=abk).status_code == 401
    client.patch(f"/api/v1/abk-accounts/{account_id}", headers=admin, json={"username": "budi", "is_active": True})
    assert client.get(url, headers=abk).status_code == 200
    employee = {"name": "Budi", "job_title": "ABK", "joined_at": "2025-01-01", "status": "INACTIVE"}
    assert client.patch(f"/api/v1/employees/{farm.employee}", headers=admin, json=employee).status_code == 200
    assert client.get(url, headers=abk).status_code == 401


def test_wrong_pins_lock_the_account_until_reset(
    client: TestClient, admin: dict[str, str], abk: dict[str, str]
) -> None:
    url = "/api/v1/abk/form-options"
    wrong = abk | {"X-ABK-PIN": "000000" if abk["X-ABK-PIN"] != "000000" else "111111"}
    for _ in range(5):
        assert client.get(url, headers=wrong).status_code == 401
    locked = client.get(url, headers=abk)  # even the right PIN is refused while locked
    assert locked.status_code == 429
    assert locked.json()["code"] == "ABK_PIN_LOCKED"
    with SessionLocal() as db:
        account = db.scalar(select(AbkAccount))
        assert account is not None and account.locked_until is not None
        assert account.locked_until > datetime.now(UTC) + timedelta(minutes=14)
    assert client.get("/api/v1/abk-accounts", headers=admin).json()["data"][0]["is_locked"] is True

    reset = client.post(f"/api/v1/abk-accounts/{account.id}/reset-pin", headers=admin)
    assert reset.status_code == 200 and reset.json()["data"]["is_locked"] is False
    new_pin = reset.json()["data"]["pin"]
    if new_pin != abk["X-ABK-PIN"]:
        assert client.get(url, headers=abk).status_code == 401  # the old PIN no longer works
    assert client.get(url, headers=abk | {"X-ABK-PIN": new_pin}).status_code == 200


def test_lock_expires_and_a_correct_pin_clears_the_counter(client: TestClient, abk: dict[str, str]) -> None:
    url = "/api/v1/abk/form-options"
    wrong = abk | {"X-ABK-PIN": "000000" if abk["X-ABK-PIN"] != "000000" else "111111"}
    for _ in range(4):
        client.get(url, headers=wrong)
    assert client.get(url, headers=abk).status_code == 200
    assert client.get(url, headers=wrong).status_code == 401  # counter restarted: 1 of 5
    with SessionLocal() as db:
        account = db.scalar(select(AbkAccount))
        assert account is not None and account.failed_pin_attempts == 1 and account.locked_until is None
        account.locked_until = datetime.now(UTC) - timedelta(seconds=1)  # an expired lock
        db.commit()
    assert client.get(url, headers=abk).status_code == 200


# --- Submitting -------------------------------------------------------------------------------


def test_form_options_list_only_active_groups_and_feeds(client: TestClient, abk: dict[str, str], farm: Farm) -> None:
    data = client.get("/api/v1/abk/form-options", headers=abk).json()["data"]
    assert data["employee"]["id"] == farm.employee
    assert {group["id"] for group in data["livestock_groups"]} == {farm.ayam_group, farm.lele_group}
    assert {feed["id"] for feed in data["feed_types"]} == {farm.ayam_feed, farm.lele_feed}


def test_submitted_report_is_a_draft_that_changes_nothing(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    response = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm))
    assert response.status_code == 201, response.text
    report = response.json()["data"]
    assert report["status"] == "DRAFT"
    assert report["group"]["species"] == "AYAM"
    assert report["feeds"][0]["quantity_kg"] == "5.750"
    assert report["eggs"]["UTUH"] == {"weight_kg": "5.000", "quantity_pieces": 80}
    assert report["reviewed_at"] is None and report["rejection_reason"] is None
    assert not {"employee", "reviewed_by", "daily_record_id", "egg_sales"} & set(report)  # staff-only fields

    assert daily_record_count() == 0
    with SessionLocal() as db:
        assert feed_balance(db, farm.ayam_feed, date(2026, 2, 1)) == Decimal("100.000")
    group = client.get(f"/api/v1/livestock-groups/{farm.ayam_group}", headers=admin).json()["data"]
    assert group["current_population"] == 100
    eggs = client.get("/api/v1/inventory/eggs?as_of=2026-02-01", headers=admin).json()["data"]["stock"]
    assert all(row["quantity_pieces"] == 0 for row in eggs)
    queue = client.get("/api/v1/abk-daily-reports?status=DRAFT", headers=admin).json()
    assert [row["id"] for row in queue["data"]] == [report["id"]]
    assert queue["data"][0]["employee"] == {"id": farm.employee, "name": "Budi"}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"eggs": None}, "eggs.UTUH"),
        ({"feeds": [{"feed_type_id": "LELE", "morning_kg": "1", "noon_kg": "1"}]}, "feeds.0.feed_type_id"),
        ({"feeds": []}, "feeds"),
        ({"mortality": -1}, "mortality"),
        ({"mortality": 101}, "mortality"),
        ({"record_date": "2025-12-31"}, "record_date"),
        ({"group_id": 999999}, "group_id"),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_submission_validation(
    client: TestClient, abk: dict[str, str], farm: Farm, change: dict[str, object], field: str
) -> None:
    response = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm, **change))
    assert response.status_code == 422, response.text
    assert field in response.json()["errors"], response.json()


def test_submission_uses_the_daily_record_rules(client: TestClient, abk: dict[str, str], farm: Farm) -> None:
    url = "/api/v1/abk/daily-reports"
    feed = {"feed_type_id": farm.ayam_feed, "morning_kg": "1", "noon_kg": "1"}
    cases = [
        (ayam_report(farm, group_id=farm.closed_group, record_date="2025-06-01"), "group_id"),
        (ayam_report(farm, feeds=[feed | {"evening_kg": "1"}]), "feeds.0.evening_kg"),
        (ayam_report(farm, feeds=[feed, feed]), "feeds.1.feed_type_id"),
        (ayam_report(farm, feeds=[feed | {"feed_type_id": farm.lele_feed}]), "feeds.0.feed_type_id"),
        (lele_report(farm, eggs=ayam_report(farm)["eggs"]), "eggs"),
    ]
    for payload, field in cases:
        response = client.post(url, headers=abk, json=payload)
        assert response.status_code == 422, response.text
        assert field in response.json()["errors"], (field, response.json())
    assert client.post(url, headers=abk, json=lele_report(farm)).status_code == 201


def test_one_pending_report_per_group_and_date(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    url = "/api/v1/abk/daily-reports"
    assert client.post(url, headers=abk, json=ayam_report(farm)).status_code == 201
    pending = client.post(url, headers=abk, json=ayam_report(farm))
    assert pending.status_code == 409 and pending.json()["code"] == "ABK_REPORT_PENDING"
    # Another date of the same group is fine.
    assert client.post(url, headers=abk, json=ayam_report(farm, record_date="2026-02-02")).status_code == 201
    # A date that already has an official daily record is refused.
    official = {"species": "LELE", "rows": [lele_report(farm)]}
    assert client.post("/api/v1/daily-records/bulk", headers=admin, json=official).status_code == 201
    exists = client.post(url, headers=abk, json=lele_report(farm))
    assert exists.status_code == 409 and exists.json()["code"] == "DAILY_RECORD_EXISTS"


# --- Review -----------------------------------------------------------------------------------


def test_approval_creates_the_daily_record_and_applies_it(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    report_id = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    response = client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=admin)
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["status"] == "APPROVED"
    assert report["reviewed_by"]["name"] == "Admin" and report["reviewed_at"]
    record = client.get(f"/api/v1/daily-records/{report['daily_record_id']}", headers=admin).json()["data"]
    assert record["record_date"] == "2026-02-01"
    assert record["mortality"] == 2
    assert record["notes"] == "Hujan deras"
    assert record["feeds"][0]["quantity_kg"] == "5.750"
    assert record["eggs"]["PECAH"] == {"weight_kg": "0.500", "quantity_pieces": 8}

    with SessionLocal() as db:
        assert feed_balance(db, farm.ayam_feed, date(2026, 2, 1)) == Decimal("94.250")
        created_by = db.scalar(select(DailyRecord.created_by))
        admin_id = db.scalar(select(User.id))
        assert created_by == admin_id  # the approver owns the official record
    group = client.get(f"/api/v1/livestock-groups/{farm.ayam_group}", headers=admin).json()["data"]
    assert group["current_population"] == 98

    again = client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=admin)
    assert again.status_code == 409 and again.json()["code"] == "ABK_REPORT_NOT_DRAFT"
    reject = client.post(f"/api/v1/abk-daily-reports/{report_id}/reject", headers=admin, json={"reason": "x"})
    assert reject.status_code == 409
    assert daily_record_count() == 1


def test_approving_a_lele_report_rebuilds_the_biomass(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    report_id = client.post("/api/v1/abk/daily-reports", headers=abk, json=lele_report(farm)).json()["data"]["id"]
    assert client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=admin).status_code == 200
    with SessionLocal() as db:
        snapshot = db.get(LeleBiomassSnapshot, farm.lele_group)
        assert snapshot is not None and snapshot.current_population == 985


def test_approval_failure_leaves_the_report_as_draft(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    report_id = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    # Meanwhile an admin records the same group and date directly.
    official = {"species": "AYAM", "rows": [ayam_report(farm)]}
    assert client.post("/api/v1/daily-records/bulk", headers=admin, json=official).status_code == 201
    response = client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=admin)
    assert response.status_code == 409 and response.json()["code"] == "DAILY_RECORD_EXISTS"
    assert client.get(f"/api/v1/abk-daily-reports/{report_id}", headers=admin).json()["data"]["status"] == "DRAFT"
    assert daily_record_count() == 1


def test_approval_revalidates_against_the_current_group(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    report_id = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    closed = client.post(
        f"/api/v1/livestock-groups/{farm.ayam_group}/close", headers=admin, json={"closed_at": "2026-03-01"}
    )
    assert closed.status_code == 200
    response = client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=admin)
    assert response.status_code == 422
    assert "group_id" in response.json()["errors"]  # not "rows.0.group_id": a single report
    assert daily_record_count() == 0


def test_rejection_keeps_it_out_and_allows_a_corrected_resubmission(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    url = "/api/v1/abk/daily-reports"
    report_id = client.post(url, headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    assert client.post(f"/api/v1/abk-daily-reports/{report_id}/reject", headers=admin, json={}).status_code == 422
    response = client.post(
        f"/api/v1/abk-daily-reports/{report_id}/reject", headers=admin, json={"reason": "Mortality salah"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "REJECTED"
    assert response.json()["data"]["rejection_reason"] == "Mortality salah"
    assert daily_record_count() == 0

    corrected = client.post(url, headers=abk, json=ayam_report(farm, mortality=1))
    assert corrected.status_code == 201
    listed = client.get("/api/v1/abk-daily-reports", headers=admin, params={"employee_id": farm.employee}).json()
    assert [row["status"] for row in listed["data"]] == ["DRAFT", "REJECTED"]
    only_rejected = client.get("/api/v1/abk-daily-reports?status=REJECTED&species=AYAM", headers=admin).json()
    assert only_rejected["meta"]["total"] == 1


def test_review_actions_are_audited(client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm) -> None:
    report_id = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=admin)
    with SessionLocal() as db:
        actions = db.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_type == "abk_daily_reports").order_by(AuditEvent.id)
        ).all()
        assert actions == ["abk_daily_report_submitted", "abk_daily_report_approved"]
        submitted = db.scalar(select(AuditEvent).where(AuditEvent.action == "abk_daily_report_submitted"))
        assert submitted is not None and submitted.actor_id is None and submitted.reason == "abk:budi"


# --- Scopes -------------------------------------------------------------------------------------


def new_abk(client: TestClient, admin: dict[str, str], username: str, scopes: list[str]) -> dict[str, str]:
    """An ABK account with ``scopes`` for a new employee; returns its headers."""
    employee = {"name": username.title(), "job_title": "ABK", "joined_at": "2025-01-01", "status": "ACTIVE"}
    employee_id = client.post("/api/v1/employees", headers=admin, json=employee).json()["data"]["id"]
    account = client.post(
        "/api/v1/abk-accounts", headers=admin, json={"employee_id": employee_id, "username": username, "scopes": scopes}
    )
    assert account.status_code == 201, account.text
    assert account.json()["data"]["scopes"] == sorted(scopes)
    return {"X-ABK-Username": username, "X-ABK-PIN": account.json()["data"]["pin"]}


def test_account_scopes_are_required_and_validated(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    with SessionLocal() as db:
        employee = Employee(name="Citra", joined_at=date(2025, 1, 1), status="ACTIVE")
        db.add(employee)
        db.commit()
        employee_id = employee.id
    url = "/api/v1/abk-accounts"
    body = {"employee_id": employee_id, "username": "citra"}
    for scopes, accepted in ((None, False), (["SAPI"], False), (["AYAM", "AYAM"], False), ([], True)):
        payload = body if scopes is None else body | {"scopes": scopes}
        response = client.post(url, headers=admin, json=payload)
        if accepted:
            assert response.status_code == 201 and response.json()["data"]["scopes"] == []
        else:
            assert response.status_code == 422, response.text
            assert any(key.startswith("scopes") for key in response.json()["errors"]), response.text


def test_scopes_are_listed_filtered_and_updated(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    new_abk(client, admin, "ayam.only", ["AYAM"])
    new_abk(client, admin, "both", ["LELE", "AYAM"])
    new_abk(client, admin, "none", [])
    new_abk(client, admin, "lele.only", ["LELE"])

    def usernames(scope: str) -> list[str]:
        rows = client.get(f"/api/v1/abk-accounts?scope={scope}", headers=admin).json()["data"]
        return [row["username"] for row in rows]

    assert usernames("AYAM") == ["ayam.only", "both"]
    assert usernames("LELE") == ["both", "lele.only"]
    assert usernames("NONE") == ["none"]

    with SessionLocal() as db:
        account_id = db.scalar(select(AbkAccount.id).where(AbkAccount.username == "ayam.only"))
    url = f"/api/v1/abk-accounts/{account_id}"
    kept = client.patch(url, headers=admin, json={"username": "ayam.only", "is_active": True})
    assert kept.json()["data"]["scopes"] == ["AYAM"]  # omitted: unchanged
    changed = client.patch(url, headers=admin, json={"username": "ayam.only", "is_active": True, "scopes": ["LELE"]})
    assert changed.json()["data"]["scopes"] == ["LELE"]
    cleared = client.patch(url, headers=admin, json={"username": "ayam.only", "is_active": True, "scopes": []})
    assert cleared.json()["data"]["scopes"] == []
    with SessionLocal() as db:
        last = db.scalar(
            select(AuditEvent).where(AuditEvent.entity_type == "abk_accounts").order_by(AuditEvent.id.desc())
        )
        assert last is not None and last.before is not None and last.after is not None
        assert (last.before["scope_lele"], last.after["scope_lele"]) == (True, False)


def test_form_options_follow_the_scopes(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    lele = client.get("/api/v1/abk/form-options", headers=new_abk(client, admin, "lele.only", ["LELE"])).json()["data"]
    assert lele["scopes"] == ["LELE"] and lele["can_report_egg_sales"] is False
    assert [group["id"] for group in lele["livestock_groups"]] == [farm.lele_group]
    assert [feed["id"] for feed in lele["feed_types"]] == [farm.lele_feed]

    ayam = client.get("/api/v1/abk/form-options", headers=new_abk(client, admin, "ayam.only", ["AYAM"])).json()["data"]
    assert ayam["can_report_egg_sales"] is True
    assert {group["species"] for group in ayam["livestock_groups"]} == {"AYAM"}

    # Without a scope the PIN still works, so the app can tell the ABK to ask for an assignment.
    response = client.get("/api/v1/abk/form-options", headers=new_abk(client, admin, "nobody", []))
    assert response.status_code == 200
    assert response.json()["data"] | {"employee": None} == {
        "scopes": [],
        "can_report_egg_sales": False,
        "employee": None,
        "livestock_groups": [],
        "feed_types": [],
    }


def test_reports_outside_the_scopes_are_forbidden(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    url = "/api/v1/abk/daily-reports"
    lele_only = new_abk(client, admin, "lele.only", ["LELE"])
    forbidden = client.post(url, headers=lele_only, json=ayam_report(farm))
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "ABK_SCOPE_FORBIDDEN" and forbidden.json()["scopes"] == ["LELE"]
    assert client.post(url, headers=lele_only, json=lele_report(farm)).status_code == 201

    nobody = new_abk(client, admin, "nobody", [])
    for payload in (ayam_report(farm, record_date="2026-02-02"), lele_report(farm, record_date="2026-02-02")):
        assert client.post(url, headers=nobody, json=payload).json()["code"] == "ABK_SCOPE_FORBIDDEN"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AbkDailyReport)) == 1


def test_scope_changes_apply_to_the_next_request(client: TestClient, admin: dict[str, str], farm: Farm) -> None:
    headers = new_abk(client, admin, "both", ["AYAM", "LELE"])
    url = "/api/v1/abk/daily-reports"
    draft_id = client.post(url, headers=headers, json=ayam_report(farm)).json()["data"]["id"]
    with SessionLocal() as db:
        account_id = db.scalar(select(AbkAccount.id).where(AbkAccount.username == "both"))
    client.patch(
        f"/api/v1/abk-accounts/{account_id}",
        headers=admin,
        json={"username": "both", "is_active": True, "scopes": ["LELE"]},
    )
    again = client.post(url, headers=headers, json=ayam_report(farm, record_date="2026-02-02"))
    assert again.status_code == 403
    # A report submitted while in scope stays reviewable: approval is the Admin's decision.
    assert client.post(f"/api/v1/abk-daily-reports/{draft_id}/approve", headers=admin).status_code == 200


# --- ABK history --------------------------------------------------------------------------------

ABK_VIEW_FIELDS = {
    "id",
    "status",
    "group",
    "record_date",
    "mortality",
    "notes",
    "feeds",
    "eggs",
    "egg_outflows",
    "submitted_at",
    "reviewed_at",
    "rejection_reason",
}


def test_abk_sees_only_their_own_reports(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    url = "/api/v1/abk/daily-reports"
    mine = client.post(url, headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    other = new_abk(client, admin, "andi", ["AYAM", "LELE"])
    theirs = client.post(url, headers=other, json=lele_report(farm)).json()["data"]["id"]

    listed = client.get(url, headers=abk).json()
    assert [row["id"] for row in listed["data"]] == [mine] and listed["meta"]["total"] == 1
    assert [row["id"] for row in client.get(url, headers=other).json()["data"]] == [theirs]

    assert client.get(f"{url}/{mine}", headers=abk).status_code == 200
    foreign = client.get(f"{url}/{theirs}", headers=abk)
    missing = client.get(f"{url}/999999", headers=abk)
    assert foreign.status_code == missing.status_code == 404  # existence is not revealed
    assert foreign.json() == missing.json()


def test_history_needs_abk_credentials(client: TestClient, admin: dict[str, str], abk: dict[str, str]) -> None:
    url = "/api/v1/abk/daily-reports"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=abk | {"X-ABK-PIN": "x"}).status_code == 401
    assert client.get(url, headers=admin).status_code == 401  # a staff token is not an ABK


def test_history_shows_the_review_outcome_without_staff_details(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    url = "/api/v1/abk/daily-reports"
    rejected = client.post(url, headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    client.post(f"/api/v1/abk-daily-reports/{rejected}/reject", headers=admin, json={"reason": "Mortality salah"})
    approved = client.post(url, headers=abk, json=ayam_report(farm, mortality=1)).json()["data"]["id"]
    client.post(f"/api/v1/abk-daily-reports/{approved}/approve", headers=admin)
    draft = client.post(url, headers=abk, json=lele_report(farm)).json()["data"]["id"]

    rows = {row["id"]: row for row in client.get(url, headers=abk).json()["data"]}
    assert all(set(row) == ABK_VIEW_FIELDS for row in rows.values())
    assert rows[rejected]["status"] == "REJECTED"
    assert rows[rejected]["rejection_reason"] == "Mortality salah" and rows[rejected]["reviewed_at"]
    assert rows[approved]["status"] == "APPROVED" and rows[approved]["reviewed_at"]
    assert rows[approved]["mortality"] == 1 and rows[approved]["feeds"][0]["feed_name"] == "BR 1"
    assert rows[draft]["status"] == "DRAFT" and rows[draft]["reviewed_at"] is None
    assert set(client.get(f"{url}/{approved}", headers=abk).json()["data"]) == ABK_VIEW_FIELDS


def test_history_filters_and_pages(client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm) -> None:
    url = "/api/v1/abk/daily-reports"
    ids = {}
    for day in ("2026-02-01", "2026-02-02", "2026-02-03"):
        ids[day] = client.post(url, headers=abk, json=ayam_report(farm, record_date=day)).json()["data"]["id"]
    lele = client.post(url, headers=abk, json=lele_report(farm)).json()["data"]["id"]
    client.post(f"/api/v1/abk-daily-reports/{ids['2026-02-02']}/reject", headers=admin, json={"reason": "x"})

    def listed(query: str) -> list[int]:
        return [row["id"] for row in client.get(f"{url}?{query}", headers=abk).json()["data"]]

    assert listed("") == [ids["2026-02-03"], ids["2026-02-02"], lele, ids["2026-02-01"]]  # newest date first
    assert listed("status=REJECTED") == [ids["2026-02-02"]]
    assert listed("species=LELE") == [lele]
    assert listed("species=AYAM&from=2026-02-02&to=2026-02-02") == [ids["2026-02-02"]]
    page = client.get(f"{url}?per_page=2&page=2", headers=abk).json()
    assert [row["id"] for row in page["data"]] == [lele, ids["2026-02-01"]]
    assert page["meta"] == {"page": 2, "per_page": 2, "total": 4, "pages": 2}
    assert client.get(f"{url}?status=DONE", headers=abk).status_code == 422


def test_history_stays_visible_after_the_scope_is_removed(
    client: TestClient, admin: dict[str, str], abk: dict[str, str], farm: Farm
) -> None:
    url = "/api/v1/abk/daily-reports"
    report_id = client.post(url, headers=abk, json=ayam_report(farm)).json()["data"]["id"]
    with SessionLocal() as db:
        account_id = db.scalar(select(AbkAccount.id).where(AbkAccount.username == "budi"))
    body = {"username": "budi", "is_active": True, "scopes": []}
    client.patch(f"/api/v1/abk-accounts/{account_id}", headers=admin, json=body)
    assert [row["id"] for row in client.get(url, headers=abk).json()["data"]] == [report_id]
