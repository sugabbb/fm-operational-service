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
    abk_security = [{"AbkUsername": [], "AbkPin": []}]
    assert paths["/api/v1/abk/daily-reports"]["post"]["security"] == abk_security
    assert paths["/api/v1/abk/form-options"]["get"]["security"] == abk_security
    assert paths["/api/v1/abk/daily-reports"]["get"]["security"] == abk_security
    assert paths["/api/v1/abk/daily-reports/{report_id}"]["get"]["security"] == abk_security
    assert paths["/api/v1/abk-daily-reports/{report_id}/approve"]["post"]["security"] == [{"BearerAuth": []}]
    assert schema["components"]["securitySchemes"]["AbkPin"] == {"type": "apiKey", "in": "header", "name": "X-ABK-PIN"}
    # Read-only pass-through of finance-service customers for the ABK egg-outflow picker; customers
    # are owned and managed by finance-service only.
    finance_reads = {"/api/v1/abk/customers"}
    assert set(paths["/api/v1/abk/customers"]) == {"get"}
    assert not any(
        fragment in path
        for path in paths
        if path not in finance_reads
        for fragment in ("revenues", "operational-cashflows", "cash-statements", "loans", "customers")
    )
