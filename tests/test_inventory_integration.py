from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import hash_password
from app.db.models import AuditEvent, ExternalInventorySource, FeedType, User
from app.db.session import SessionLocal
from app.features.farm import feed_balance
from app.main import app

INTERNAL_HEADERS = {"X-FM-Internal-Key": "test-internal-key-with-at-least-24-characters"}
HUMAN_API_KEY = "test-api-key-with-at-least-24-characters"
PASSWORD = "StrongPassword123"


@pytest.fixture
def seeded(clean_database: None) -> tuple[int, int]:
    with SessionLocal() as db:
        ayam = FeedType(code="AYAM-BR-1", species="AYAM", name="BR 1", is_active=True)
        lele = FeedType(code="LELE-781", species="LELE", name="781", is_active=True)
        user = User(
            name="Operations Admin",
            email="operations@example.com",
            password_hash=hash_password(PASSWORD),
            role="SUPERADMIN",
            is_active=True,
            must_change_password=False,
        )
        db.add_all([ayam, lele, user])
        db.commit()
        return ayam.id, lele.id


def login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        headers={"X-FM-API-Key": HUMAN_API_KEY},
        json={"email": "operations@example.com", "password": PASSWORD},
    )
    assert response.status_code == 200
    return {
        "X-FM-API-Key": HUMAN_API_KEY,
        "Authorization": f"Bearer {response.json()['data']['access_token']}",
    }


def test_feed_receipt_revision_idempotency_correction_and_void(seeded: tuple[int, int]) -> None:
    ayam_id, _ = seeded
    url = "/api/v1/internal/inventory/feed-receipts/cashflow-10"
    payload = {
        "source_service": "finance-backend",
        "source_revision": 1,
        "status": "ACTIVE",
        "received_date": "2026-11-17",
        "lines": [{"source_line_id": "line-1", "feed_type_code": "AYAM-BR-1", "quantity_kg": "100"}],
    }
    with TestClient(app) as client:
        assert client.put(url, json=payload).status_code == 401
        first = client.put(url, headers=INTERNAL_HEADERS, json=payload)
        assert first.status_code == 200
        formatted_retry = payload | {
            "lines": [{"source_line_id": "line-1", "feed_type_code": "AYAM-BR-1", "quantity_kg": "100.000"}]
        }
        assert client.put(url, headers=INTERNAL_HEADERS, json=formatted_retry).status_code == 200

        conflict = client.put(url, headers=INTERNAL_HEADERS, json=payload | {"status": "VOIDED"})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "SOURCE_REVISION_CONFLICT"

        corrected = payload | {
            "source_revision": 2,
            "lines": [{"source_line_id": "line-1", "feed_type_code": "AYAM-BR-1", "quantity_kg": "80"}],
        }
        assert client.put(url, headers=INTERNAL_HEADERS, json=corrected).status_code == 200
        stale = client.put(url, headers=INTERNAL_HEADERS, json=payload)
        assert stale.status_code == 409
        assert stale.json()["code"] == "STALE_SOURCE_REVISION"

        with SessionLocal() as db:
            assert feed_balance(db, ayam_id, date(2026, 11, 17)) == Decimal("80.000")

        voided = {
            "source_service": "finance-backend",
            "source_revision": 3,
            "status": "VOIDED",
            "received_date": "2026-11-17",
            "lines": [],
        }
        assert client.put(url, headers=INTERNAL_HEADERS, json=voided).status_code == 200

        reactivated = payload | {
            "source_revision": 4,
            "lines": [{"source_line_id": "line-1", "feed_type_code": "AYAM-BR-1", "quantity_kg": "25"}],
        }
        assert client.put(url, headers=INTERNAL_HEADERS, json=reactivated).status_code == 200

        other_service = reactivated | {
            "source_service": "legacy-importer",
            "source_revision": 1,
            "lines": [{"source_line_id": "line-1", "feed_type_code": "AYAM-BR-1", "quantity_kg": "5"}],
        }
        assert client.put(url, headers=INTERNAL_HEADERS, json=other_service).status_code == 200

    with SessionLocal() as db:
        assert feed_balance(db, ayam_id, date(2026, 11, 17)) == Decimal("30.000")
        source = db.scalar(
            select(ExternalInventorySource).where(ExternalInventorySource.source_service == "finance-backend")
        )
        assert source.source_revision == 4
        assert source.status == "ACTIVE"
        audits = db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
        assert len(audits) == 5
        assert all(row.actor_id is None for row in audits)
        assert {row.reason for row in audits} == {"finance-backend", "legacy-importer"}


def test_egg_sale_updates_inventory_and_supports_as_of(seeded: tuple[int, int]) -> None:
    payload = {
        "source_service": "finance-backend",
        "source_revision": 1,
        "status": "ACTIVE",
        "transaction_date": "2026-11-17",
        "lines": [
            {
                "source_line_id": "egg-1",
                "egg_category": "UTUH",
                "weight_kg": "12.5",
                "quantity_pieces": 200,
            }
        ],
    }
    with TestClient(app) as client:
        assert (
            client.put(
                "/api/v1/internal/inventory/egg-sales/revenue-20", headers=INTERNAL_HEADERS, json=payload
            ).status_code
            == 200
        )
        headers = login(client)
        before = client.get("/api/v1/inventory/eggs?as_of=2026-11-16", headers=headers).json()["data"]["stock"]
        after = client.get("/api/v1/inventory/eggs?as_of=2026-11-17", headers=headers).json()["data"]["stock"]
    assert next(row for row in before if row["egg_category"] == "UTUH")["weight_kg"] == "0.000"
    intact = next(row for row in after if row["egg_category"] == "UTUH")
    assert intact["weight_kg"] == "-12.500"
    assert intact["quantity_pieces"] == -200


def test_invalid_feed_rolls_back_source(seeded: tuple[int, int]) -> None:
    payload = {
        "source_service": "finance-backend",
        "source_revision": 1,
        "status": "ACTIVE",
        "received_date": "2026-11-17",
        "lines": [{"source_line_id": "bad", "feed_type_code": "UNKNOWN", "quantity_kg": "1"}],
    }
    with TestClient(app) as client:
        response = client.put(
            "/api/v1/internal/inventory/feed-receipts/cashflow-bad", headers=INTERNAL_HEADERS, json=payload
        )
        assert response.status_code == 422
    with SessionLocal() as db:
        assert db.scalar(select(ExternalInventorySource)) is None
