# Farm Management Operational Service

Independent synchronous FastAPI/PostgreSQL service for non-monetary daily farm operations. It owns authentication, users, employees, livestock groups, daily records, feed and egg inventory, Lele sampling, and the Lele biomass read model. It intentionally contains no cashflow, revenue, customer, payment, loan, cash-account, statement, report, or proof-upload feature.

## Runtime and setup

- Python 3.14
- PostgreSQL
- FastAPI, SQLAlchemy, Alembic, Pydantic, JWT, and Argon2 using the same pinned versions as `backend`

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

Use an independent operational database. Share `JWT_SECRET`, `JWT_ISSUER`, and `JWT_AUDIENCE` with `finance-service`; generate `INTERNAL_API_KEY` separately and never expose it to browser code.

- Swagger: <http://127.0.0.1:8001/docs>
- OpenAPI: <http://127.0.0.1:8001/openapi.json>
- Liveness: <http://127.0.0.1:8001/health/live>
- Readiness: <http://127.0.0.1:8001/health/ready>

The database starts empty. Create the first Superadmin with an Argon2 hash generated locally:

```bash
python -c "from app.core.security import hash_password; print(hash_password(input('Initial password: ')))"
```

```sql
INSERT INTO users
    (name, email, password_hash, role, is_active, must_change_password, token_version, created_at, updated_at)
VALUES
    ('Initial Superadmin', 'superadmin@example.com', '$argon2id$REPLACE_WITH_GENERATED_HASH',
     'SUPERADMIN', TRUE, TRUE, 0, NOW(), NOW());
```

The frontend logs in here with email/password and uses the returned Bearer JWT directly against both operational-service and finance-service. Human routes require only that Bearer token. This service is the identity authority; finance calls the private authorization endpoint on every request to enforce current user state and token version.

## Internal inventory API

Machine calls use only `X-FM-Internal-Key`. `INTERNAL_API_PREVIOUS_KEY` supports zero-downtime rotation. A source is addressed by service, type, and path ID. Revisions must increase; an exact retry is a no-op, a stale revision is rejected, and reusing a revision with different content is rejected.

Feed receipt:

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/internal/inventory/feed-receipts/cashflow-42 \
  -H 'X-FM-Internal-Key: replace-with-internal-secret' \
  -H 'Content-Type: application/json' \
  -d '{
    "source_service":"finance-service",
    "source_revision":1,
    "status":"ACTIVE",
    "received_date":"2026-11-17",
    "lines":[
      {"source_line_id":"line-1","feed_type_code":"AYAM-BR-1","quantity_kg":"100.000"}
    ]
  }'
```

Egg sale:

```bash
curl -X PUT http://127.0.0.1:8001/api/v1/internal/inventory/egg-sales/revenue-84 \
  -H 'X-FM-Internal-Key: replace-with-internal-secret' \
  -H 'Content-Type: application/json' \
  -d '{
    "source_service":"finance-service",
    "source_revision":1,
    "status":"ACTIVE",
    "transaction_date":"2026-11-17",
    "lines":[
      {"source_line_id":"line-1","egg_category":"UTUH","weight_kg":"12.500","quantity_pieces":200}
    ]
  }'
```

Send the complete current line set on every higher revision. Use `status: "VOIDED"` with an empty `lines` array to remove the source's inventory effect. The service accepts no monetary, price, customer, approval, or payment fields.

Feed types expose an immutable generated `code`; integrations must use it instead of a database ID. Renaming a feed type does not change its code.

## Lele biomass jobs

Writes enqueue biomass rebuilds transactionally and normally drain them after the response. A dedicated worker is available as a reliability fallback:

```bash
python -m app.scripts.lele_biomass_worker
python -m app.scripts.lele_biomass_worker --once
```

Recompute all sampling results and biomass snapshots after direct data repairs or configuration changes:

```bash
python -m app.scripts.backfill_lele_sampling_results --dry-run
python -m app.scripts.backfill_lele_sampling_results
```

## Checks

Tests require a PostgreSQL database whose name ends in `_test`.

```bash
python -m pytest
ruff check app tests
mypy app
```
