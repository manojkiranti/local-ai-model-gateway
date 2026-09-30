"""Request/response schemas for the auth endpoints."""

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    # An email OR a bare network username — mapped to the account email by
    # app/auth/login_name.py. The field keeps its name for existing clients.
    email: str = Field(min_length=1, max_length=254)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the token expires
