from math import ceil
from typing import Annotated

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class PageParams:
    def __init__(
        self,
        page: Annotated[int, Query(ge=1)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 30,
    ) -> None:
        self.page = page
        self.per_page = per_page

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.per_page

    def meta(self, total: int) -> dict[str, int]:
        return {
            "page": self.page,
            "per_page": self.per_page,
            "total": total,
            "pages": ceil(total / self.per_page) if total else 0,
        }


PageDep = Annotated[PageParams, Depends()]
