from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntitlementPut(StrictModel):
    external_customer_id: str = Field(min_length=1, max_length=200)
    expires_at: datetime
    device_limit: int = Field(ge=1, le=10000, strict=True)
    status: Literal["active", "suspended", "revoked"]
    revision: int = Field(ge=1, le=2_147_483_647, strict=True)
    rotate_activation_key: bool = False

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value.astimezone(UTC)


class EntitlementView(StrictModel):
    external_id: str
    external_customer_id: str
    expires_at: datetime
    device_limit: int
    status: str
    revision: int
    active_devices: int
    activation_key: str | None = None
    changed: bool


class ActivationRequest(StrictModel):
    activation_key: str = Field(min_length=32, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    installation_id: UUID
    device_public_key: str = Field(min_length=43, max_length=44)


class RenewRequest(StrictModel):
    activation_id: UUID


class LicenseView(StrictModel):
    activation_id: UUID
    external_id: str
    expires_at: datetime
    device_limit: int
    revision: int
    lease: str
    renew_after_seconds: int = 30
