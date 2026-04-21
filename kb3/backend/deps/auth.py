"""
deps/auth.py — JWT authentication + role-based access (RBAC for Kịch bản 3).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.config import get_settings


bearer = HTTPBearer(auto_error=False)


@dataclass
class User:
    username: str
    role: str  # "admin" | "guest"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def authenticate_user(username: str, password: str) -> Optional[User]:
    s = get_settings()
    record = s.DEMO_USERS_PLAIN.get(username)
    if not record:
        return None
    # So sánh plain password trực tiếp (demo only — production phải dùng bcrypt).
    # Lý do: tránh phụ thuộc hash hard-coded có thể lệch giữa bcrypt versions.
    if password != record["password"]:
        return None
    return User(username=username, role=record["role"])


# ====================================================================
# JWT
# ====================================================================
def create_access_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.username,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.JWT_EXPIRE_MIN)).timestamp()),
    }
    return jwt.encode(payload, s.JWT_SECRET, algorithm=s.JWT_ALG)


def decode_token(token: str) -> Optional[User]:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.JWT_SECRET, algorithms=[s.JWT_ALG])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    username = payload.get("sub")
    role = payload.get("role", "guest")
    if not username:
        return None
    return User(username=username, role=role)


# ====================================================================
# FastAPI dependencies
# ====================================================================
async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> User:
    """Yêu cầu JWT. Raise 401 nếu không có / invalid."""
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing token")
    user = decode_token(credentials.credentials)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    return user


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> User:
    """Không bắt buộc JWT. Nếu không có thì coi như guest anonymous."""
    if not credentials:
        return User(username="anonymous", role="guest")
    user = decode_token(credentials.credentials)
    if not user:
        return User(username="anonymous", role="guest")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin required")
    return user


# ====================================================================
# Helper để route sang ES index phù hợp với role
# ====================================================================
def get_index_for_user(user: User) -> str:
    """Admin → index gốc đầy đủ. Guest → alias với filter + source excludes."""
    s = get_settings()
    return s.ES_INDEX_FULL if user.is_admin else s.ES_INDEX_PUBLIC


def get_source_excludes_for_user(user: User) -> Optional[list]:
    """Guest bị ẩn các field nhạy cảm."""
    s = get_settings()
    return None if user.is_admin else s.GUEST_EXCLUDE_FIELDS
