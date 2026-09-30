"""Request/response schemas.

Note: we deliberately avoid ``pydantic.EmailStr`` because it pulls in the
``email-validator`` package. A pragmatic regex keeps the service dependency-free;
the authoritative uniqueness check is the database index anyway.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")

PASSWORD_MIN = 8
PASSWORD_MAX = 200


def _clean_email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL_RE.match(value):
        raise ValueError("invalid email address")
    return value


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=254)
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        return _clean_email(v)


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)

    @field_validator("email")
    @classmethod
    def check_email(cls, v: str) -> str:
        return _clean_email(v)


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(default="Default key", min_length=1, max_length=60)


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    created_at: str


class ApiKeyOut(BaseModel):
    id: int
    name: str
    prefix: str
    created_at: str
    last_used_at: str | None = None
