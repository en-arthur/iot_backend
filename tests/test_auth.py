from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.auth import AuthError, JwtVerifier
from app.config import Settings

SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
ISSUER = "https://test.supabase.co/auth/v1"


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql://user:pass@localhost:5432/postgres",
        "supabase_url": "https://test.supabase.co",
        "supabase_jwt_secret": SECRET,
        "supabase_jwt_audience": "authenticated",
    }
    values.update(overrides)
    return Settings(**values)


def make_token(**claims) -> str:
    payload = {
        "sub": "11111111-1111-4111-8111-111111111111",
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "role": "authenticated",
    }
    payload.update(claims)
    return jwt.encode(payload, SECRET, algorithm="HS256")


class TestJwtVerifier:
    async def test_valid_token_returns_user_id(self):
        verifier = JwtVerifier(make_settings())
        user_id = await verifier.verify(make_token())
        assert str(user_id) == "11111111-1111-4111-8111-111111111111"

    async def test_expired_token(self):
        verifier = JwtVerifier(make_settings())
        token = make_token(exp=datetime.now(UTC) - timedelta(minutes=5))
        with pytest.raises(AuthError, match="expired"):
            await verifier.verify(token)

    async def test_wrong_audience(self):
        verifier = JwtVerifier(make_settings())
        with pytest.raises(AuthError, match="audience"):
            await verifier.verify(make_token(aud="anon"))

    async def test_wrong_issuer(self):
        verifier = JwtVerifier(make_settings())
        with pytest.raises(AuthError, match="issuer"):
            await verifier.verify(make_token(iss="https://evil.example.com/auth/v1"))

    async def test_bad_signature(self):
        verifier = JwtVerifier(make_settings())
        token = jwt.encode(
            {
                "sub": "11111111-1111-4111-8111-111111111111",
                "aud": "authenticated",
                "iss": ISSUER,
                "exp": datetime.now(UTC) + timedelta(minutes=5),
            },
            "a-different-secret-with-32-characters-minimum",
            algorithm="HS256",
        )
        with pytest.raises(AuthError, match="Invalid"):
            await verifier.verify(token)

    async def test_missing_token(self):
        verifier = JwtVerifier(make_settings())
        with pytest.raises(AuthError, match="Missing"):
            await verifier.verify("   ")

    async def test_non_uuid_subject(self):
        verifier = JwtVerifier(make_settings())
        with pytest.raises(AuthError, match="subject"):
            await verifier.verify(make_token(sub="not-a-uuid"))
