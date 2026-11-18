"""Characterization tests of the admin daily-record endpoints (validation and inventory effects)."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.db.models import FeedType, InventoryOpeningBalance, LivestockGroup, User
from app.db.session import SessionLocal
from app.features.farm import feed_balance
from app.main import app

PASSWORD = "StrongPassword123"


@dataclass(frozen=True)
class Farm:
    ayam_group: int
    lele_group: int
    ayam_feed: int
    lele_feed: int


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
        ayam = LivestockGroup(
            species="AYAM",
            code="KANDANG-1",
            name="Kandang 1",
            start_date=date(2026, 1, 1),
            initial_population=100,
            location="Blok A",
            status="ACTIVE",
        )
        lele = LivestockGroup(
            species="LELE",
            code="KOLAM-1",
            name="Kolam 1",
            start_date=date(2026, 1, 1),
            initial_population=1000,
            location="Blok B",
            status="ACTIVE",
        )
        db.add_all([admin, ayam_feed, lele_feed, ayam, lele])
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
        return Farm(ayam.id, lele.id, ayam_feed.id, lele_feed.id)


@pytest.fixture
def client(farm: Farm) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        token = test_client.post(
            "/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD}
        ).json()["data"]["access_token"]
        test_client.headers["Authorization"] = f"Bearer {token}"
        yield test_client


def ayam_row(farm: Farm, **changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "group_id": farm.ayam_group,
        "record_date": "2026-02-01",
        "mortality": 2,
        "feeds": [{"feed_type_id": farm.ayam_feed, "morning_kg": "3.5", "noon_kg": "2.25"}],
        "eggs": {
            "UTUH": {"weight_kg": "5", "quantity_pieces": 80},
            "PECAH": {"weight_kg": "0.5", "quantity_pieces": 8},
            "REJECT": {"weight_kg": "0", "quantity_pieces": 0},
        },
    }
    return row | changes


def test_bulk_create_applies_feed_usage_population_and_eggs(client: TestClient, farm: Farm) -> None:
    response = client.post("/api/v1/daily-records/bulk", json={"species": "AYAM", "rows": [ayam_row(farm)]})
    assert response.status_code == 201, response.text
    record = response.json()["data"][0]
    assert record["feeds"][0]["quantity_kg"] == "5.750"
    assert record["eggs"]["UTUH"] == {"weight_kg": "5.000", "quantity_pieces": 80}
    with SessionLocal() as db:
        assert feed_balance(db, farm.ayam_feed, date(2026, 2, 1)) == Decimal("94.250")
    group = client.get(f"/api/v1/livestock-groups/{farm.ayam_group}").json()["data"]
    assert group["current_population"] == 98
    eggs = client.get("/api/v1/inventory/eggs", params={"as_of": "2026-02-01"}).json()["data"]["stock"]
    assert next(row for row in eggs if row["egg_category"] == "UTUH")["quantity_pieces"] == 80


@pytest.mark.parametrize(
    ("species", "change", "field"),
    [
        ("AYAM", {"feeds": []}, "rows.0.feeds"),
        ("AYAM", {"eggs": None}, "eggs.UTUH"),
        (
            "AYAM",
            {"feeds": [{"feed_type_id": "LELE", "morning_kg": "1", "noon_kg": "1"}]},
            "rows.0.feeds.0.feed_type_id",
        ),
        ("AYAM", {"feeds": "evening"}, "feeds.0.evening_kg"),
        ("AYAM", {"feeds": "duplicate"}, "feeds.1.feed_type_id"),
        ("AYAM", {"feeds": "wrong-species"}, "feeds.0.feed_type_id"),
        ("AYAM", {"mortality": 101}, "population"),
        ("LELE", {"eggs": "ayam-eggs"}, "eggs"),
    ],
)
def test_bulk_create_validation(
    client: TestClient, farm: Farm, species: str, change: dict[str, object], field: str
) -> None:
    row = ayam_row(farm)
    feed = {"feed_type_id": farm.ayam_feed, "morning_kg": "1", "noon_kg": "1"}
    if species == "LELE":
        row = row | {"group_id": farm.lele_group, "feeds": [feed | {"feed_type_id": farm.lele_feed}]}
    value = next(iter(change.values()))
    key = next(iter(change))
    if value == "evening":
        row["feeds"] = [feed | {"evening_kg": "1"}]
    elif value == "duplicate":
        row["feeds"] = [feed, feed]
    elif value == "wrong-species":
        row["feeds"] = [feed | {"feed_type_id": farm.lele_feed}]
    elif value == "ayam-eggs":
        pass  # the Ayam eggs of ayam_row stay on the Lele row
    else:
        row[key] = value
    response = client.post("/api/v1/daily-records/bulk", json={"species": species, "rows": [row]})
    assert response.status_code == 422, response.text
    assert field in response.json()["errors"], response.json()


def test_bulk_create_rejects_duplicate_group_and_date(client: TestClient, farm: Farm) -> None:
    assert (
        client.post("/api/v1/daily-records/bulk", json={"species": "AYAM", "rows": [ayam_row(farm)]}).status_code == 201
    )
    again = client.post("/api/v1/daily-records/bulk", json={"species": "AYAM", "rows": [ayam_row(farm)]})
    assert again.status_code == 409
    assert again.json()["code"] == "DAILY_RECORD_EXISTS"
