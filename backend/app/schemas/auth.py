from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from .common import ContractModel

Username = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$")]


class LoginRequest(ContractModel):
    username: Username
    password: Annotated[str, Field(min_length=8, max_length=512)]
    replace_existing: bool = False


class CurrentUser(ContractModel):
    user_id: str
    username: str
    created_at: datetime


class SessionConflictResponse(ContractModel):
    code: Literal["active_session_exists"] = "active_session_exists"
    message: str = "This account is active elsewhere. Continue to sign out the other session."


class UserPreferencesResponse(ContractModel):
    preferences: dict[str, Any]


class UserPreferencesUpdate(ContractModel):
    # Preferences are deliberately an opaque, bounded document: UI settings
    # can evolve without a database migration and remain private to the user.
    preferences: dict[str, Any] = Field(default_factory=dict, max_length=100)
