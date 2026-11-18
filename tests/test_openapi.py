from fastapi.testclient import TestClient

from app.main import app


def test_openapi_security_and_boundary() -> None:
    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()
        assert client.get("/health/live").json() == {"status": "ok"}
    paths = schema["paths"]
    assert "security" not in paths["/api/v1/auth/login"]["post"]
    assert paths["/api/v1/employees"]["get"]["security"] == [{"BearerAuth": []}]
    assert paths["/api/v1/internal/inventory/feed-receipts/{source_id}"]["put"]["security"] == [
        {"InternalApiKeyAuth": []}
    ]
    assert not any(
        fragment in path
        for path in paths
        for fragment in ("revenues", "operational-cashflows", "cash-statements", "loans", "customers")
    )

