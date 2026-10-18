from typing import Literal

from app.schemas.common import APIModel


class HealthData(APIModel):
    status: Literal["ok", "ready"]
    service: str = "tw-stock-analysis-api"


class HealthResponse(APIModel):
    data: HealthData
