"""Client of finance-service's internal API (contract: ``docs/finance-internal-api.yaml``).

Only two needs: customers (finance-service owns them) for ABK egg outflows, and the delivery of
approved egg sales, which finance-service records as revenues. Failures are split in two:

- ``FinanceUnavailable``: worth retrying later (network error, timeout, 5xx, 408, 429, 401/403 from
  a key mismatch, unreadable answer, or the client is not configured).
- ``FinanceRejected``: finance-service refused the request itself (other 4xx); retrying the same
  request cannot succeed.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError

CUSTOMERS_PATH = "/api/v1/internal/customers"
EGG_SALE_REVENUES_PATH = "/api/v1/internal/revenues/egg-sales"
RETRYABLE_STATUS = {401, 403, 408, 429}

# Tests replace it with an httpx.MockTransport; None uses the network.
transport: httpx.BaseTransport | None = None


class FinanceUnavailable(Exception):
    pass


class FinanceRejected(Exception):
    def __init__(self, status_code: int, code: str, detail: str, errors: dict[str, list[str]]) -> None:
        super().__init__(f"finance-service answered {status_code} {code}: {detail}")
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.errors = errors


@dataclass(frozen=True)
class Customer:
    id: int
    name: str
    is_active: bool


def _request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    if not settings.finance_base_url or not settings.finance_internal_api_key:
        raise FinanceUnavailable("finance-service is not configured (FINANCE_BASE_URL, FINANCE_INTERNAL_API_KEY)")
    try:
        with httpx.Client(
            base_url=settings.finance_base_url.rstrip("/"),
            headers={"X-FM-Internal-Key": settings.finance_internal_api_key, "Accept": "application/json"},
            timeout=settings.finance_timeout_seconds,
            transport=transport,
        ) as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise FinanceUnavailable(f"finance-service request failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code >= 500 or response.status_code in RETRYABLE_STATUS:
        raise FinanceUnavailable(f"finance-service answered {response.status_code}: {response.text[:500]}")
    return response


def _rejected(response: httpx.Response) -> FinanceRejected:
    try:
        body = response.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    errors = body.get("errors")
    return FinanceRejected(
        response.status_code,
        str(body.get("code") or "FINANCE_REJECTED"),
        str(body.get("detail") or response.text[:500]),
        errors if isinstance(errors, dict) else {},
    )


def _data(response: httpx.Response) -> Any:
    try:
        return response.json()["data"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FinanceUnavailable("finance-service answered with an unreadable body") from exc


def _customer(item: Any) -> Customer:
    try:
        return Customer(id=int(item["id"]), name=str(item["name"]), is_active=bool(item["is_active"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise FinanceUnavailable("finance-service answered with an unreadable customer") from exc


def list_active_customers(q: str | None, page: int, per_page: int) -> tuple[list[Customer], dict[str, int]]:
    params: dict[str, str | int] = {"active_only": "true", "page": page, "per_page": per_page}
    if q:
        params["q"] = q
    response = _request("GET", CUSTOMERS_PATH, params=params)
    if response.status_code != 200:
        raise _rejected(response)
    try:
        body = response.json()
        items, meta = body["data"], body["meta"]
        page_meta = {key: int(meta[key]) for key in ("page", "per_page", "total", "pages")}
    except (ValueError, KeyError, TypeError) as exc:
        raise FinanceUnavailable("finance-service answered with an unreadable customer list") from exc
    return [_customer(item) for item in items], page_meta


def get_customer(customer_id: int) -> Customer | None:
    response = _request("GET", f"{CUSTOMERS_PATH}/{customer_id}")
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _rejected(response)
    return _customer(_data(response))


def put_egg_sale_revenue(source_id: str, payload: dict[str, Any]) -> str:
    """Delivers one approved egg sale; returns finance-service's revenue id."""
    response = _request("PUT", f"{EGG_SALE_REVENUES_PATH}/{source_id}", json=payload)
    if response.status_code not in (200, 201):
        raise _rejected(response)
    data = _data(response)
    try:
        return str(data["revenue_id"])
    except (KeyError, TypeError) as exc:
        raise FinanceUnavailable("finance-service answered without a revenue id") from exc


def active_customer(customer_id: int, field: str) -> Customer:
    """For request handlers: the customer, or the API error to return."""
    try:
        customer = get_customer(customer_id)
    except FinanceUnavailable as exc:
        raise unavailable() from exc
    except FinanceRejected as exc:
        raise AppError(502, "FINANCE_ERROR", "The finance service rejected the request.") from exc
    if customer is None or not customer.is_active:
        raise AppError(
            422,
            "VALIDATION_ERROR",
            "The request is invalid.",
            errors={field: ["Customer does not exist or is inactive."]},
        )
    return customer


def unavailable() -> AppError:
    return AppError(503, "FINANCE_UNAVAILABLE", "The finance service is unavailable. Try again later.")
