"""ABK egg outflow: submission, Superadmin approval with prices, and delivery to finance-service.

finance-service is replaced by ``FakeFinance``, which implements ``docs/finance-internal-api.yaml``:
every request operational-service sends (and every answer the fake gives) is validated against
that contract, so the spec handed to the finance team and this client cannot drift apart.
"""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate as validate_openapi
from sqlalchemy import func, select

from app.core.config import settings
from app.core.security import hash_password
from app.db.models import AbkAccount, AbkEggSale, DailyRecord, Employee, FeedType, LivestockGroup, User
from app.db.session import SessionLocal
from app.features import finance_client
from app.features.abk_egg_sales import backoff, deliver_pending
from app.features.common import now_utc
from app.main import app
from app.scripts import finance_egg_sale_worker

SPEC = yaml.safe_load((Path(__file__).parents[1] / "docs" / "finance-internal-api.yaml").read_text())
PASSWORD = "StrongPassword123"
INTERNAL_HEADERS = {"X-FM-Internal-Key": "test-internal-key-with-at-least-24-characters"}


def validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$ref": f"#/components/schemas/{name}", "components": SPEC["components"]}, format_checker=FormatChecker()
    )


def envelope(name: str, *, page: bool = False) -> Draft202012Validator:
    data: dict[str, Any] = (
        {"type": "array", "items": {"$ref": f"#/components/schemas/{name}"}}
        if page
        else {"$ref": f"#/components/schemas/{name}"}
    )
    properties = {"data": data} | ({"meta": {"$ref": "#/components/schemas/PageMeta"}} if page else {})
    return Draft202012Validator(
        {"type": "object", "required": list(properties), "properties": properties, "components": SPEC["components"]}
    )


REVENUE_INPUT = validator("EggSaleRevenueInput")
REVENUE_ANSWER = envelope("EggSaleRevenueResult")
CUSTOMER_ANSWER = envelope("CustomerRef")
CUSTOMER_PAGE = envelope("CustomerRef", page=True)
SOURCE_ID = re.compile(
    SPEC["paths"]["/api/v1/internal/revenues/egg-sales/{source_id}"]["put"]["parameters"][0]["schema"]["pattern"]
)


@dataclass
class FakeFinance:
    customers: dict[int, dict[str, Any]] = field(
        default_factory=lambda: {
            12: {"id": 12, "name": "Toko Sumber Rejeki", "is_active": True},
            13: {"id": 13, "name": "Warung Bu Siti", "is_active": True},
            14: {"id": 14, "name": "Pengepul Lama", "is_active": False},
        }
    )
    revenues: dict[str, dict[str, Any]] = field(default_factory=dict)
    puts: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    down: bool = False
    lose_next_response: bool = False
    violations: list[str] = field(default_factory=list)

    def answer(self, status: int, body: dict[str, Any], schema: Draft202012Validator | None = None) -> httpx.Response:
        if schema is not None:
            self.violations += [f"answer: {error.message}" for error in schema.iter_errors(body)]
        return httpx.Response(status, json=body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.down:
            raise httpx.ConnectError("finance-service is down", request=request)
        if request.headers.get("X-FM-Internal-Key") != settings.finance_internal_api_key:
            return self.answer(401, {"code": "INVALID_INTERNAL_API_KEY", "detail": "invalid"})
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/internal/customers":
            rows = sorted(self.customers.values(), key=lambda row: row["name"])
            if request.url.params.get("active_only") == "true":
                rows = [row for row in rows if row["is_active"]]
            if q := request.url.params.get("q"):
                rows = [row for row in rows if q.lower() in row["name"].lower()]
            meta = {"page": 1, "per_page": 30, "total": len(rows), "pages": 1 if rows else 0}
            return self.answer(200, {"data": rows, "meta": meta}, CUSTOMER_PAGE)
        if request.method == "GET" and (match := re.fullmatch(r"/api/v1/internal/customers/(\d+)", path)):
            row = self.customers.get(int(match[1]))
            if row is None:
                return self.answer(404, {"code": "NOT_FOUND", "detail": "Customer was not found."})
            return self.answer(200, {"data": row}, CUSTOMER_ANSWER)
        if request.method == "PUT" and (match := re.fullmatch(r"/api/v1/internal/revenues/egg-sales/(.+)", path)):
            return self.put_revenue(match[1], json.loads(request.content), request)
        self.violations.append(f"unexpected request {request.method} {path}")
        return self.answer(404, {"code": "NOT_FOUND", "detail": "no route"})

    def put_revenue(self, source_id: str, body: dict[str, Any], request: httpx.Request) -> httpx.Response:
        self.puts.append(body)
        problems = [error.message for error in REVENUE_INPUT.iter_errors(body)]
        if not SOURCE_ID.fullmatch(source_id):
            problems.append(f"bad source id {source_id}")
        if problems:
            self.violations += problems
            return self.answer(422, {"code": "VALIDATION_ERROR", "detail": "contract", "errors": {"body": problems}})
        customer = self.customers.get(body["customer_id"])
        if not customer or not customer["is_active"]:
            return self.answer(
                422,
                {"code": "VALIDATION_ERROR", "detail": "invalid", "errors": {"customer_id": ["Customer is inactive."]}},
            )
        stored = self.revenues.get(source_id)
        if stored and body["source_revision"] < stored["body"]["source_revision"]:
            return self.answer(409, {"code": "STALE_SOURCE_REVISION", "detail": "stale"})
        if stored and body["source_revision"] == stored["body"]["source_revision"] and body != stored["body"]:
            return self.answer(409, {"code": "SOURCE_REVISION_CONFLICT", "detail": "conflict"})
        revenue_id = stored["revenue_id"] if stored else str(84 + len(self.revenues))
        self.revenues[source_id] = {"revenue_id": revenue_id, "body": body}
        if self.lose_next_response:  # stored, but the answer never reaches operational-service
            self.lose_next_response = False
            raise httpx.ReadTimeout("timed out", request=request)
        return self.answer(
            200, {"data": {"revenue_id": revenue_id, "source_revision": body["source_revision"]}}, REVENUE_ANSWER
        )


@dataclass(frozen=True)
class Farm:
    ayam_group: int
    lele_group: int
    ayam_feed: int
    lele_feed: int
    employee: int


@pytest.fixture
def finance(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeFinance]:
    fake = FakeFinance()
    monkeypatch.setattr(finance_client, "transport", httpx.MockTransport(fake.handle))
    yield fake
    assert fake.violations == [], fake.violations


@pytest.fixture
def farm(clean_database: None) -> Farm:
    with SessionLocal() as db:
        for role in ("ADMIN", "SUPERADMIN"):
            db.add(
                User(
                    name=role.title(),
                    email=f"{role.lower()}@example.com",
                    password_hash=hash_password(PASSWORD),
                    role=role,
                    is_active=True,
                    must_change_password=False,
                )
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
        employee = Employee(name="Budi", job_title="ABK", joined_at=date(2025, 1, 1), status="ACTIVE")
        db.add_all([ayam_feed, lele_feed, ayam, lele, employee])
        db.commit()
        return Farm(ayam.id, lele.id, ayam_feed.id, lele_feed.id, employee.id)


@pytest.fixture
def client(farm: Farm, finance: FakeFinance) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def staff(client: TestClient, role: str) -> dict[str, str]:
    response = client.post("/api/v1/auth/login", json={"email": f"{role}@example.com", "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
def superadmin(client: TestClient) -> dict[str, str]:
    return staff(client, "superadmin")


@pytest.fixture
def admin(client: TestClient) -> dict[str, str]:
    return staff(client, "admin")


@pytest.fixture
def abk(client: TestClient, admin: dict[str, str], farm: Farm) -> dict[str, str]:
    response = client.post(
        "/api/v1/abk-accounts",
        headers=admin,
        json={"employee_id": farm.employee, "username": "budi", "scopes": ["AYAM", "LELE"]},
    )
    return {"X-ABK-Username": "budi", "X-ABK-PIN": response.json()["data"]["pin"]}


OUTFLOW = {
    "customer_id": 12,
    "lines": [
        {"egg_category": "PECAH", "quantity_pieces": 15, "weight_kg": "0.9"},
        {"egg_category": "UTUH", "quantity_pieces": 200, "weight_kg": "12.5"},
    ],
}
SALE = {
    "customer_id": 13,  # corrected by the Superadmin
    "lines": [
        {"egg_category": "UTUH", "quantity_pieces": 198, "weight_kg": "12.400", "price_per_kg": "28000"},
        {"egg_category": "PECAH", "quantity_pieces": 15, "weight_kg": "0.9", "price_per_kg": "18000.50"},
    ],
}


def ayam_report(farm: Farm, **changes: object) -> dict[str, object]:
    report: dict[str, object] = {
        "group_id": farm.ayam_group,
        "record_date": "2026-02-01",
        "mortality": 1,
        "feeds": [{"feed_type_id": farm.ayam_feed, "morning_kg": "3", "noon_kg": "2"}],
        "eggs": {
            "UTUH": {"weight_kg": "14", "quantity_pieces": 220},
            "PECAH": {"weight_kg": "1", "quantity_pieces": 16},
            "REJECT": {"weight_kg": "0", "quantity_pieces": 0},
        },
        "egg_outflows": [OUTFLOW],
    }
    return report | changes


def submit(client: TestClient, abk: dict[str, str], payload: dict[str, object]) -> dict[str, Any]:
    response = client.post("/api/v1/abk/daily-reports", headers=abk, json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json()["data"])


def egg_stock(client: TestClient, headers: dict[str, str]) -> dict[str, int]:
    rows = client.get("/api/v1/inventory/eggs?as_of=2026-02-01", headers=headers).json()["data"]["stock"]
    return {row["egg_category"]: row["quantity_pieces"] for row in rows}


def sale_row() -> AbkEggSale:
    with SessionLocal() as db:
        sale = db.scalar(select(AbkEggSale))
        assert sale is not None
        return sale


def test_the_finance_contract_is_a_valid_openapi_document() -> None:
    validate_openapi(SPEC)


def test_backoff_doubles_up_to_an_hour() -> None:
    assert [backoff(n) for n in (1, 2, 3)] == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4)]
    assert backoff(7) == backoff(50) == timedelta(hours=1)


# --- ABK side -----------------------------------------------------------------------------------


def test_abk_customer_picker_shows_active_customers_only(
    client: TestClient, abk: dict[str, str], finance: FakeFinance
) -> None:
    response = client.get("/api/v1/abk/customers", headers=abk)
    assert response.status_code == 200
    assert response.json()["data"] == [
        {"id": 12, "name": "Toko Sumber Rejeki"},
        {"id": 13, "name": "Warung Bu Siti"},
    ]
    assert client.get("/api/v1/abk/customers?q=siti", headers=abk).json()["data"] == [
        {"id": 13, "name": "Warung Bu Siti"}
    ]
    assert client.get("/api/v1/abk/customers").status_code == 401
    finance.down = True
    down = client.get("/api/v1/abk/customers", headers=abk)
    assert down.status_code == 503 and down.json()["code"] == "FINANCE_UNAVAILABLE"


def test_unconfigured_finance_is_reported_as_unavailable(
    client: TestClient, abk: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "finance_base_url", "")
    assert client.get("/api/v1/abk/customers", headers=abk).json()["code"] == "FINANCE_UNAVAILABLE"


def test_outflow_is_stored_as_reported_and_changes_nothing(
    client: TestClient, abk: dict[str, str], admin: dict[str, str], farm: Farm, finance: FakeFinance
) -> None:
    report = submit(client, abk, ayam_report(farm))
    assert report["status"] == "DRAFT"
    assert report["egg_outflows"] == [
        {
            "customer": {"id": 12, "name": "Toko Sumber Rejeki"},
            "lines": [
                {"egg_category": "UTUH", "quantity_pieces": 200, "weight_kg": "12.500"},
                {"egg_category": "PECAH", "quantity_pieces": 15, "weight_kg": "0.900"},
            ],
        }
    ]
    assert "egg_sales" not in report  # staff-only
    assert finance.puts == []
    assert egg_stock(client, admin) == {"UTUH": 0, "PECAH": 0, "REJECT": 0}
    pending = client.get("/api/v1/abk-daily-reports?status=DRAFT&has_egg_outflow=true", headers=admin).json()
    assert [row["id"] for row in pending["data"]] == [report["id"]]
    assert client.get("/api/v1/abk-daily-reports?has_egg_outflow=false", headers=admin).json()["meta"]["total"] == 0


def test_outflow_is_optional_and_then_finance_is_not_needed(
    client: TestClient, abk: dict[str, str], farm: Farm, finance: FakeFinance
) -> None:
    finance.down = True
    report = submit(client, abk, ayam_report(farm, egg_outflows=None))
    assert report["egg_outflows"] == []
    submit(client, abk, ayam_report(farm, record_date="2026-02-03", egg_outflows=[]))
    payload = ayam_report(farm, record_date="2026-02-02")
    del payload["egg_outflows"]
    submit(client, abk, payload)
    assert finance.requests == 0


@pytest.mark.parametrize(
    ("outflow", "field"),
    [
        ({"customer_id": 12, "lines": []}, "egg_outflows.0.lines"),
        ({"customer_id": 12, "lines": [OUTFLOW["lines"][0], OUTFLOW["lines"][0]]}, "egg_outflows.0"),  # type: ignore[index]
        (
            {"customer_id": 12, "lines": [{"egg_category": "UTUH", "quantity_pieces": 0, "weight_kg": "1"}]},
            "egg_outflows.0.lines.0.quantity_pieces",
        ),
        (
            {"customer_id": 12, "lines": [{"egg_category": "UTUH", "quantity_pieces": 5, "weight_kg": "0"}]},
            "egg_outflows.0.lines.0.weight_kg",
        ),
        (
            {"customer_id": 12, "lines": [{"egg_category": "UTUH", "quantity_pieces": 5, "weight_kg": "1.2345"}]},
            "egg_outflows.0.lines.0.weight_kg",
        ),
        (
            {"customer_id": 12, "lines": [{"egg_category": "BESAR", "quantity_pieces": 5, "weight_kg": "1"}]},
            "egg_outflows.0.lines.0.egg_category",
        ),
        (
            {"customer_id": 12, "lines": [{"egg_category": "UTUH", "weight_kg": "1"}]},
            "egg_outflows.0.lines.0.quantity_pieces",
        ),
        ({"customer_id": 99, "lines": OUTFLOW["lines"]}, "egg_outflows.0.customer_id"),
        ({"customer_id": 14, "lines": OUTFLOW["lines"]}, "egg_outflows.0.customer_id"),  # inactive customer
    ],
)
def test_outflow_validation(
    client: TestClient, abk: dict[str, str], farm: Farm, outflow: dict[str, object], field: str
) -> None:
    response = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm, egg_outflows=[outflow]))
    assert response.status_code == 422, response.text
    assert field in response.json()["errors"], response.json()


def test_outflow_is_for_ayam_only(client: TestClient, abk: dict[str, str], farm: Farm) -> None:
    lele = {
        "group_id": farm.lele_group,
        "record_date": "2026-02-01",
        "mortality": 0,
        "feeds": [{"feed_type_id": farm.lele_feed, "morning_kg": "1", "noon_kg": "1"}],
        "egg_outflows": [OUTFLOW],
    }
    response = client.post("/api/v1/abk/daily-reports", headers=abk, json=lele)
    assert response.status_code == 422 and "egg_outflows" in response.json()["errors"]


def test_outflow_cannot_be_submitted_while_finance_is_down(
    client: TestClient, abk: dict[str, str], farm: Farm, finance: FakeFinance
) -> None:
    finance.down = True
    response = client.post("/api/v1/abk/daily-reports", headers=abk, json=ayam_report(farm))
    assert response.status_code == 503
    finance.down = False
    submit(client, abk, ayam_report(farm))  # nothing was stored by the failed attempt


# --- Approval -----------------------------------------------------------------------------------


def test_only_a_superadmin_with_prices_can_approve_an_outflow(
    client: TestClient,
    abk: dict[str, str],
    admin: dict[str, str],
    superadmin: dict[str, str],
    farm: Farm,
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]
    url = f"/api/v1/abk-daily-reports/{report_id}/approve"
    as_admin = client.post(url, headers=admin, json={"egg_sales": [SALE]})
    assert as_admin.status_code == 403 and as_admin.json()["code"] == "SUPERADMIN_REQUIRED"
    no_prices = client.post(url, headers=superadmin)
    assert no_prices.status_code == 422 and "egg_sales" in no_prices.json()["errors"]
    empty = client.post(url, headers=superadmin, json={"egg_sales": []})
    assert empty.status_code == 422 and "egg_sales" in empty.json()["errors"]
    missing_price = {"customer_id": 12, "lines": [{"egg_category": "UTUH", "quantity_pieces": 1, "weight_kg": "1"}]}
    response = client.post(url, headers=superadmin, json={"egg_sales": [missing_price]})
    assert response.status_code == 422 and "egg_sales.0.lines.0.price_per_kg" in response.json()["errors"]
    too_precise = {"customer_id": 12, "lines": [SALE["lines"][0] | {"price_per_kg": "1.001"}]}  # type: ignore[index]
    assert client.post(url, headers=superadmin, json={"egg_sales": [too_precise]}).status_code == 422
    inactive = client.post(url, headers=superadmin, json={"egg_sales": [SALE, SALE | {"customer_id": 14}]})
    assert inactive.status_code == 422 and "egg_sales.1.customer_id" in inactive.json()["errors"]
    assert client.get(f"/api/v1/abk-daily-reports/{report_id}", headers=admin).json()["data"]["status"] == "DRAFT"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(DailyRecord)) == 0


def test_a_report_without_outflow_takes_no_sale(
    client: TestClient, abk: dict[str, str], admin: dict[str, str], superadmin: dict[str, str], farm: Farm
) -> None:
    report_id = submit(client, abk, ayam_report(farm, egg_outflows=None))["id"]
    url = f"/api/v1/abk-daily-reports/{report_id}/approve"
    response = client.post(url, headers=superadmin, json={"egg_sales": [SALE]})
    assert response.status_code == 422 and "egg_sales" in response.json()["errors"]
    assert client.post(url, headers=admin).status_code == 200  # Admins still approve these


def test_approval_with_corrections_is_delivered_to_finance(
    client: TestClient,
    abk: dict[str, str],
    admin: dict[str, str],
    superadmin: dict[str, str],
    farm: Farm,
    finance: FakeFinance,
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]
    response = client.post(
        f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": [SALE]}
    )
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert report["status"] == "APPROVED"
    # What the ABK reported is kept as reported.
    assert report["egg_outflows"][0]["customer"]["id"] == 12
    assert report["egg_outflows"][0]["lines"][0]["quantity_pieces"] == 200
    (approved,) = report["egg_sales"]
    # The response is built before the delivery that runs after it.
    assert approved["customer"] == {"id": 13, "name": "Warung Bu Siti"}
    assert approved["lines"] == [
        {"egg_category": "UTUH", "quantity_pieces": 198, "weight_kg": "12.400", "price_per_kg": "28000.00"},
        {"egg_category": "PECAH", "quantity_pieces": 15, "weight_kg": "0.900", "price_per_kg": "18000.50"},
    ]
    assert approved["approved_by"]["name"] == "Superadmin"

    # Delivered right after the response, in the contract's shape.
    (sent,) = finance.puts
    assert sent == {
        "source_service": "operational-service",
        "source_revision": 1,
        "transaction_date": "2026-02-01",
        "customer_id": 13,
        "lines": [
            {
                "source_line_id": "UTUH",
                "egg_category": "UTUH",
                "quantity_pieces": 198,
                "weight_kg": "12.400",
                "price_per_kg": "28000.00",
            },
            {
                "source_line_id": "PECAH",
                "egg_category": "PECAH",
                "quantity_pieces": 15,
                "weight_kg": "0.900",
                "price_per_kg": "18000.50",
            },
        ],
        "origin": {
            "abk_report_id": report_id,
            "daily_record_id": report["daily_record_id"],
            "submitted_by_employee_id": farm.employee,
            "approved_by_user_id": approved["approved_by"]["id"],
            "approved_at": sent["origin"]["approved_at"],
        },
    }
    sale = client.get(f"/api/v1/abk-egg-sales/{approved['id']}", headers=admin).json()["data"]
    assert sale["finance"]["status"] == "SENT"
    assert sale["finance"]["revenue_id"] == "84"
    assert sale["finance"]["source_id"] == f"abk-egg-sale-{sale['id']}"
    assert sale["finance"]["attempts"] == 1

    # Approval changes the egg stock only by the harvest; the sale comes back from finance-service
    # through the existing inventory API, as for any egg revenue.
    assert egg_stock(client, admin) == {"UTUH": 220, "PECAH": 16, "REJECT": 0}
    callback = {
        "source_service": "finance-service",
        "source_revision": 1,
        "status": "ACTIVE",
        "transaction_date": "2026-02-01",
        "lines": [
            {
                "source_line_id": line["source_line_id"],
                "egg_category": line["egg_category"],
                "weight_kg": line["weight_kg"],
                "quantity_pieces": line["quantity_pieces"],
            }
            for line in sent["lines"]
        ],
    }
    assert (
        client.put(
            "/api/v1/internal/inventory/egg-sales/revenue-84", headers=INTERNAL_HEADERS, json=callback
        ).status_code
        == 200
    )
    assert egg_stock(client, admin) == {"UTUH": 22, "PECAH": 1, "REJECT": 0}


def test_approval_does_not_depend_on_delivery(
    client: TestClient,
    abk: dict[str, str],
    admin: dict[str, str],
    superadmin: dict[str, str],
    farm: Farm,
    finance: FakeFinance,
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]

    failed_once = False

    def fail_the_first_delivery(request: httpx.Request) -> httpx.Response:
        nonlocal failed_once
        if request.method == "PUT" and not failed_once:
            failed_once = True
            finance.down = True  # the customer check passed; finance-service goes down right after
        return finance.handle(request)

    finance_client.transport = httpx.MockTransport(fail_the_first_delivery)
    response = client.post(
        f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": [SALE]}
    )
    assert response.status_code == 200
    sale = sale_row()
    assert sale.finance_status == "PENDING" and sale.attempts == 1
    assert sale.last_error and "ConnectError" in sale.last_error
    assert sale.next_attempt_at is not None and sale.next_attempt_at > now_utc()

    finance.down = False
    assert deliver_pending() == 0  # still backing off
    with SessionLocal() as db:
        db.get_one(AbkEggSale, sale.id).next_attempt_at = now_utc() - timedelta(seconds=1)
        db.commit()
    assert finance_egg_sale_worker.main(["--once"]) == 0
    sale = sale_row()
    assert sale.finance_status == "SENT" and sale.attempts == 2 and sale.last_error is None
    assert finance.puts[-1]["source_revision"] == 1


def test_finance_down_at_approval_changes_nothing(
    client: TestClient, abk: dict[str, str], superadmin: dict[str, str], farm: Farm, finance: FakeFinance
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]
    finance.down = True
    response = client.post(
        f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": [SALE]}
    )
    assert response.status_code == 503
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(DailyRecord)) == 0
        assert db.scalar(select(func.count()).select_from(AbkEggSale)) == 0


def test_a_lost_response_is_retried_without_duplicating_the_revenue(
    client: TestClient, abk: dict[str, str], superadmin: dict[str, str], farm: Farm, finance: FakeFinance
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]
    finance.lose_next_response = True
    client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": [SALE]})
    assert sale_row().finance_status == "PENDING"
    assert len(finance.revenues) == 1  # finance-service stored it; operational-service does not know

    with SessionLocal() as db:
        db.get_one(AbkEggSale, sale_row().id).next_attempt_at = None
        db.commit()
    assert deliver_pending() == 1
    assert sale_row().finance_status == "SENT"
    assert len(finance.revenues) == 1
    assert finance.puts[0] == finance.puts[1]  # an identical retry: a no-op for finance-service


def test_a_rejected_sale_is_corrected_and_resent_as_a_new_revision(
    client: TestClient,
    abk: dict[str, str],
    admin: dict[str, str],
    superadmin: dict[str, str],
    farm: Farm,
    finance: FakeFinance,
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]

    def deactivate_before_delivery(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and not finance.puts:
            finance.customers[13]["is_active"] = False  # deactivated in finance meanwhile
        return finance.handle(request)

    finance_client.transport = httpx.MockTransport(deactivate_before_delivery)
    client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": [SALE]})
    sale = client.get("/api/v1/abk-egg-sales?finance_status=FAILED", headers=admin).json()["data"][0]
    assert "Customer is inactive" in sale["finance"]["last_error"]

    url = f"/api/v1/abk-egg-sales/{sale['id']}/resend"
    assert client.post(url, headers=admin).status_code == 403
    corrected = SALE | {"customer_id": 12}
    response = client.post(url, headers=superadmin, json=corrected)
    assert response.status_code == 200
    assert response.json()["data"]["customer"]["name"] == "Toko Sumber Rejeki"
    assert response.json()["data"]["finance"]["source_revision"] == 2

    delivered = client.get(f"/api/v1/abk-egg-sales/{sale['id']}", headers=admin).json()["data"]
    assert delivered["finance"]["status"] == "SENT"
    assert finance.puts[-1]["source_revision"] == 2 and finance.puts[-1]["customer_id"] == 12
    sent_again = client.post(url, headers=superadmin)
    assert sent_again.status_code == 409 and sent_again.json()["code"] == "EGG_SALE_ALREADY_SENT"


# --- Several customers per day ----------------------------------------------------------------

SITI = {"customer_id": 13, "lines": [{"egg_category": "REJECT", "quantity_pieces": 30, "weight_kg": "1.5"}]}


def test_abk_reports_outflows_to_several_customers(
    client: TestClient, abk: dict[str, str], farm: Farm, finance: FakeFinance
) -> None:
    report = submit(client, abk, ayam_report(farm, egg_outflows=[OUTFLOW, SITI]))
    assert [entry["customer"] for entry in report["egg_outflows"]] == [
        {"id": 12, "name": "Toko Sumber Rejeki"},
        {"id": 13, "name": "Warung Bu Siti"},
    ]
    assert report["egg_outflows"][1]["lines"] == [
        {"egg_category": "REJECT", "quantity_pieces": 30, "weight_kg": "1.500"}
    ]
    # The same egg category may go to several customers; the same customer only once per report.
    both_utuh = SITI | {"lines": [{"egg_category": "UTUH", "quantity_pieces": 10, "weight_kg": "0.6"}]}
    submit(client, abk, ayam_report(farm, record_date="2026-02-02", egg_outflows=[OUTFLOW, both_utuh]))

    url = "/api/v1/abk/daily-reports"
    twice = client.post(
        url, headers=abk, json=ayam_report(farm, record_date="2026-02-03", egg_outflows=[OUTFLOW, OUTFLOW])
    )
    assert twice.status_code == 422 and "egg_outflows" in twice.json()["errors"]
    inactive = client.post(
        url,
        headers=abk,
        json=ayam_report(farm, record_date="2026-02-03", egg_outflows=[OUTFLOW, SITI | {"customer_id": 14}]),
    )
    assert inactive.status_code == 422 and "egg_outflows.1.customer_id" in inactive.json()["errors"]
    many = [{"customer_id": 100 + n, "lines": OUTFLOW["lines"]} for n in range(21)]
    too_many = client.post(url, headers=abk, json=ayam_report(farm, record_date="2026-02-03", egg_outflows=many))
    assert too_many.status_code == 422 and "egg_outflows" in too_many.json()["errors"]
    assert finance.puts == []


def test_one_revenue_per_customer_and_the_superadmin_may_add_customers(
    client: TestClient,
    abk: dict[str, str],
    admin: dict[str, str],
    superadmin: dict[str, str],
    farm: Farm,
    finance: FakeFinance,
) -> None:
    report_id = submit(client, abk, ayam_report(farm, egg_outflows=[OUTFLOW, SITI]))["id"]
    finance.customers[15] = {"id": 15, "name": "Pak Joko", "is_active": True}
    sales = [
        {
            "customer_id": 12,
            "lines": [{"egg_category": "UTUH", "quantity_pieces": 200, "weight_kg": "12.5", "price_per_kg": "28000"}],
        },
        {
            "customer_id": 13,
            "lines": [{"egg_category": "REJECT", "quantity_pieces": 30, "weight_kg": "1.5", "price_per_kg": "9000"}],
        },
        # Not in the ABK report: the Superadmin knows of another buyer that day.
        {
            "customer_id": 15,
            "lines": [{"egg_category": "PECAH", "quantity_pieces": 15, "weight_kg": "0.9", "price_per_kg": "18000"}],
        },
    ]
    url = f"/api/v1/abk-daily-reports/{report_id}/approve"
    duplicate = client.post(url, headers=superadmin, json={"egg_sales": [sales[0], sales[0]]})
    assert duplicate.status_code == 422 and "egg_sales" in duplicate.json()["errors"]

    response = client.post(url, headers=superadmin, json={"egg_sales": sales})
    assert response.status_code == 200, response.text
    report = response.json()["data"]
    assert [sale["customer"]["id"] for sale in report["egg_sales"]] == [12, 13, 15]
    assert len(report["egg_outflows"]) == 2  # what the ABK reported stays as reported

    # Three sources, three revenues, all from the same daily report.
    source_ids = {sale["finance"]["source_id"] for sale in report["egg_sales"]}
    assert source_ids == set(finance.revenues) and len(source_ids) == 3
    assert sorted(put["customer_id"] for put in finance.puts) == [12, 13, 15]
    assert {put["origin"]["abk_report_id"] for put in finance.puts} == {report_id}
    listed = client.get(f"/api/v1/abk-daily-reports/{report_id}", headers=admin).json()["data"]["egg_sales"]
    assert [sale["finance"]["status"] for sale in listed] == ["SENT", "SENT", "SENT"]
    assert len({sale["finance"]["revenue_id"] for sale in listed}) == 3


def test_each_customer_sale_is_delivered_and_corrected_on_its_own(
    client: TestClient,
    abk: dict[str, str],
    admin: dict[str, str],
    superadmin: dict[str, str],
    farm: Farm,
    finance: FakeFinance,
) -> None:
    report_id = submit(client, abk, ayam_report(farm, egg_outflows=[OUTFLOW, SITI]))["id"]

    def deactivate_toko_before_delivery(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and not finance.puts:
            finance.customers[12]["is_active"] = False  # the first sale delivered is rejected
        return finance.handle(request)

    finance_client.transport = httpx.MockTransport(deactivate_toko_before_delivery)
    sales = [OUTFLOW_PRICED, SITI_PRICED]
    client.post(f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": sales})
    toko, siti = client.get(f"/api/v1/abk-daily-reports/{report_id}", headers=admin).json()["data"]["egg_sales"]
    assert toko["finance"]["status"] == "FAILED"
    assert siti["finance"]["status"] == "SENT"  # not held back by the other customer's rejection

    url = f"/api/v1/abk-egg-sales/{toko['id']}/resend"
    taken = client.post(url, headers=superadmin, json=OUTFLOW_PRICED | {"customer_id": 13})
    assert taken.status_code == 409 and taken.json()["code"] == "EGG_SALE_CUSTOMER_EXISTS"
    finance.customers[15] = {"id": 15, "name": "Pak Joko", "is_active": True}
    moved = client.post(url, headers=superadmin, json=OUTFLOW_PRICED | {"customer_id": 15})
    assert moved.status_code == 200
    toko = client.get(f"/api/v1/abk-egg-sales/{toko['id']}", headers=admin).json()["data"]
    assert toko["finance"]["status"] == "SENT" and toko["finance"]["source_revision"] == 2
    assert toko["customer"] == {"id": 15, "name": "Pak Joko"}
    assert len(finance.revenues) == 2
    assert sum(1 for put in finance.puts if put["customer_id"] == 13) == 1  # Siti was sent once


OUTFLOW_PRICED = {
    "customer_id": 12,
    "lines": [line | {"price_per_kg": "28000"} for line in OUTFLOW["lines"]],  # type: ignore[attr-defined]
}
SITI_PRICED = {
    "customer_id": 13,
    "lines": [line | {"price_per_kg": "9000"} for line in SITI["lines"]],  # type: ignore[attr-defined]
}


def test_customer_picker_needs_the_chicken_scope(
    client: TestClient, abk: dict[str, str], admin: dict[str, str], finance: FakeFinance
) -> None:
    with SessionLocal() as db:
        account_id = db.scalar(select(AbkAccount.id))
    body = {"username": "budi", "is_active": True, "scopes": ["LELE"]}
    client.patch(f"/api/v1/abk-accounts/{account_id}", headers=admin, json=body)
    response = client.get("/api/v1/abk/customers", headers=abk)
    assert response.status_code == 403 and response.json()["code"] == "ABK_SCOPE_FORBIDDEN"
    assert finance.requests == 0  # refused before asking finance-service


def test_abk_history_shows_their_outflows_never_the_approved_sales(
    client: TestClient, abk: dict[str, str], superadmin: dict[str, str], farm: Farm
) -> None:
    report_id = submit(client, abk, ayam_report(farm))["id"]
    approve = client.post(
        f"/api/v1/abk-daily-reports/{report_id}/approve", headers=superadmin, json={"egg_sales": [SALE]}
    )
    assert approve.status_code == 200
    mine = client.get(f"/api/v1/abk/daily-reports/{report_id}", headers=abk).json()["data"]
    assert mine["status"] == "APPROVED"
    # What the ABK entered (customer 12, 200 UTUH), not the Superadmin's correction (customer 13, 198).
    assert mine["egg_outflows"][0]["customer"]["id"] == 12
    assert mine["egg_outflows"][0]["lines"][0]["quantity_pieces"] == 200
    assert "egg_sales" not in mine
    assert "price" not in json.dumps(mine) and "revenue" not in json.dumps(mine)
