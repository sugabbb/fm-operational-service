from typing import Any


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        errors: dict[str, list[str]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.errors = errors or {}
        self.extra = extra or {}


def conflict(code: str, detail: str, **extra: Any) -> AppError:
    return AppError(409, code, detail, extra=extra)


def forbidden(detail: str = "You are not allowed to perform this action.") -> AppError:
    return AppError(403, "FORBIDDEN", detail)


def not_found(resource: str) -> AppError:
    return AppError(404, "NOT_FOUND", f"{resource} was not found.")


def validation(field: str, detail: str) -> AppError:
    return AppError(422, "VALIDATION_ERROR", "The request is invalid.", errors={field: [detail]})
