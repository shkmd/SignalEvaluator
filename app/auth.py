"""Email/password auth with server-side sessions (httponly cookie).

Password hashing uses stdlib PBKDF2-HMAC-SHA256 rather than bcrypt/argon2 -- no compiled
dependency, so it can't hit the kind of missing-wheel build failure pandas did on Railway.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request, HTTPException

from app import db

SESSION_COOKIE = "session_token"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, hex_digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), hex_digest)


def create_session_for_user(user_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.create_session(token, user_id, expires_at.isoformat())
    return token, expires_at


def get_user_from_request(request: Request) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    session = db.get_session(token)
    if not session:
        return None
    expires_at = datetime.fromisoformat(session["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        db.delete_session(token)
        return None
    return db.get_user_by_id(session["user_id"])


def require_user(request: Request) -> dict:
    user = get_user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user
