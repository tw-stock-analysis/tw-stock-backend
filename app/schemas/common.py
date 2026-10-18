from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class FieldError(APIModel):
    location: list[str | int]
    message: str


class ErrorDetail(APIModel):
    code: str
    message: str
    field_errors: list[FieldError] = Field(default_factory=list)
    request_id: str


class ErrorResponse(APIModel):
    error: ErrorDetail


class Pagination(APIModel):
    page: int
    limit: int
    total: int
