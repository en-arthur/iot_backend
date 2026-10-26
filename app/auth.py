"""Supabase JWT verification and the ``current user`` dependency.

The frontend signs users in with Supabase Auth and sends the resulting access
token as ``Authorization: Bearer <token>``. This backend validates that token
locally — it never calls the Supabase auth server per request:

* legacy projects: HS256 with ``SUPABASE_JWT_SECRET``
* new projects: asymmetric keys fetched from the project JWKS endpoint
  (cached, refreshed when an unknown ``kid`` appears)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any
from uuid import UUID

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_JWKS_REFRESH_COOLDOWN = 5.0  # seconds between forced refreshes for unknown kids


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired, or unverifiable."""


class JwtVerifier:
    """Verify Supabase access tokens and return the subject (user id)."""

    def __init__(self, settings: Settings) -> None:
        self._secret = settings.supabase_jwt_secret
        self._jwks_url = settings.resolved_jwks_url
        self._audience = settings.supabase_jwt_audience
        self._issuer = settings.resolved_issuer
        self._cache_ttl = settings.jwks_cache_ttl

        self._keys: dict[str, Any] = {}
        self._keys_fetched_at = 0.0
        self._last_forced_refresh = 0.0
        self._lock = asyncio.Lock()

    async def verify(self, token: str) -> UUID:
        """Return the user id from a valid token, or raise :class:`AuthError`."""
        if not token or not token.strip():
            raise AuthError("Missing authentication token")

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid authentication token") from exc

        algorithm = header.get("alg")
        if not algorithm or algorithm.lower() == "none":
            raise AuthError("Invalid authentication token")

        if self._secret:
            if not algorithm.startswith("HS"):
                raise AuthError("Invalid authentication token")
            key: Any = self._secret
        else:
            key = await self._signing_key(header.get("kid"), algorithm)

        try:
            payload = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": ["exp", "sub", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Authentication token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AuthError("Authentication token has the wrong audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Authentication token has the wrong issuer") from exc
        except jwt.PyJWTError as exc:
            raise AuthError("Invalid authentication token") from exc

        subject = payload.get("sub")
        if not subject:
            raise AuthError("Authentication token is missing a subject")

        try:
            return UUID(str(subject))
        except (TypeError, ValueError) as exc:
            raise AuthError("Authentication token has an invalid subject") from exc

    async def _signing_key(self, kid: str | None, algorithm: str) -> Any:
        now = time.monotonic()
        if kid and kid in self._keys and now - self._keys_fetched_at < self._cache_ttl:
            return self._keys[kid]

        async with self._lock:
            # Another task may have refreshed while we waited for the lock.
            cache_is_fresh = time.monotonic() - self._keys_fetched_at < self._cache_ttl
            if kid and kid in self._keys and cache_is_fresh:
                return self._keys[kid]

            now = time.monotonic()
            needs_refresh = (
                not self._keys
                or kid not in self._keys
                or now - self._keys_fetched_at >= self._cache_ttl
            )
            # An unknown kid is usually key rotation, but do not let a flood of
            # bad tokens hammer the JWKS endpoint.
            if kid not in self._keys and now - self._last_forced_refresh < _JWKS_REFRESH_COOLDOWN:
                needs_refresh = False

            if needs_refresh:
                self._last_forced_refresh = now
                await self._refresh_keys()

            if kid:
                key = self._keys.get(kid)
            elif len(self._keys) == 1:
                key = next(iter(self._keys.values()))
            else:
                key = None

            if key is None:
                raise AuthError("Authentication token has an unknown signing key")

            return key

    async def _refresh_keys(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self._jwks_url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.error(
                "could not fetch Supabase JWKS",
                extra={"jwks_url": self._jwks_url, "error": str(exc)},
            )
            raise AuthError("Could not verify authentication token") from exc

        keys: dict[str, Any] = {}
        for jwk in payload.get("keys", []):
            kid = jwk.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(jwk).key
            except (jwt.PyJWTError, ValueError) as exc:
                logger.warning(
                    "skipping unusable JWKS key",
                    extra={"kid": kid, "error": str(exc)},
                )

        if not keys:
            raise AuthError("Could not verify authentication token")

        self._keys = keys
        self._keys_fetched_at = time.monotonic()
        logger.info("refreshed Supabase JWKS", extra={"key_count": len(keys)})


def build_verifier(settings: Settings | None = None) -> JwtVerifier:
    return JwtVerifier(settings or get_settings())


_bearer = HTTPBearer(auto_error=False, description="Supabase access token")


async def get_current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UUID:
    """FastAPI dependency: validate the bearer token and return the user id."""
    verifier: JwtVerifier | None = getattr(request.app.state, "jwt_verifier", None)
    if verifier is None:  # pragma: no cover - misconfiguration, not a request bug
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured.",
        )

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        return await verifier.verify(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentUser = Annotated[UUID, Depends(get_current_user_id)]
