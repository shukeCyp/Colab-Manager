"""Pydantic data models for colab-manager."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AccountRecord(BaseModel):
    """Metadata stored in accounts.json for one managed Google account."""

    alias: str
    email: Optional[str] = None
    note: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RegistryState(BaseModel):
    """Root registry file (~/.config/colab-manager/accounts.json)."""

    version: int = 1
    default_account: Optional[str] = None
    accounts: Dict[str, AccountRecord] = Field(default_factory=dict)


class CcuFullInfo(BaseModel):
    """Full response from GET /tun/m/ccu-info including eligible accelerators."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    current_balance: float = Field(0.0, alias="currentBalance")
    consumption_rate_hourly: float = Field(0.0, alias="consumptionRateHourly")
    assignments_count: int = Field(0, alias="assignmentsCount")
    eligible_gpus: List[str] = Field(default_factory=list, alias="eligibleGpus")
    eligible_tpus: List[str] = Field(default_factory=list, alias="eligibleTpus")


class AssignmentDetail(BaseModel):
    """One active remote Colab runtime assignment."""

    alias: str
    email: Optional[str] = None
    session_name: Optional[str] = None
    endpoint: str
    accelerator: str = "CPU"
    machine_shape: str = "Standard"
    variant: str = "DEFAULT"
    token_expires_in_seconds: Optional[int] = None


class AccountUsageReport(BaseModel):
    """Combined health, balance, burn rate, accelerators, and assignments for one account."""

    alias: str
    email: Optional[str] = None
    note: str = ""
    is_default: bool = False
    ok: bool = True
    error: Optional[str] = None
    balance: Optional[float] = None
    rate_hourly: Optional[float] = None
    assignments_count: int = 0
    eligible_gpus: List[str] = Field(default_factory=list)
    eligible_tpus: List[str] = Field(default_factory=list)
    assignments: List[AssignmentDetail] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=utc_now)

    @property
    def estimated_hours_left(self) -> Optional[float]:
        if self.balance is None or self.rate_hourly is None:
            return None
        if self.rate_hourly <= 0:
            return None
        return max(0.0, self.balance / self.rate_hourly)


class AuthCheckReport(BaseModel):
    """Token inspection result for one account."""

    alias: str
    recorded_email: Optional[str] = None
    live_email: Optional[str] = None
    is_default: bool = False
    ok: bool = True
    error: Optional[str] = None
    expires_in_seconds: Optional[int] = None
    has_refresh_token: bool = False
    missing_scopes: List[str] = Field(default_factory=list)
    scopes: List[str] = Field(default_factory=list)
