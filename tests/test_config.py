from __future__ import annotations

from app.config import Settings
from app.db import build_engine_options, normalize_database_url


def make_settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql://user:pass@localhost:5432/postgres",
        "supabase_url": "https://test.supabase.co",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class TestCorsOrigins:
    def test_comma_separated(self):
        settings = make_settings(cors_origins="http://a, http://b")
        assert settings.cors_origins == ["http://a", "http://b"]

    def test_json_array(self):
        settings = make_settings(cors_origins='["http://a", "http://b"]')
        assert settings.cors_origins == ["http://a", "http://b"]

    def test_default_allows_next_dev_server(self):
        assert make_settings().cors_origins == ["http://localhost:3000"]


class TestOptionalValues:
    def test_blank_secret_becomes_none(self):
        settings = make_settings(supabase_jwt_secret="   ", mqtt_password="")
        assert settings.supabase_jwt_secret is None
        assert settings.mqtt_password is None

    def test_default_jwks_and_issuer(self):
        settings = make_settings()
        assert settings.resolved_jwks_url == (
            "https://test.supabase.co/auth/v1/.well-known/jwks.json"
        )
        assert settings.resolved_issuer == "https://test.supabase.co/auth/v1"

    def test_explicit_jwks_and_issuer_win(self):
        settings = make_settings(
            supabase_jwks_url="https://example.com/jwks",
            supabase_jwt_issuer="https://example.com/auth/v1",
        )
        assert settings.resolved_jwks_url == "https://example.com/jwks"
        assert settings.resolved_issuer == "https://example.com/auth/v1"


class TestDatabaseUrl:
    def test_adds_asyncpg_driver(self):
        url = normalize_database_url("postgres://user:pass@db.example.com:5432/postgres")
        assert url.drivername == "postgresql+asyncpg"
        assert url.host == "db.example.com"

    def test_keeps_asyncpg_driver(self):
        url = normalize_database_url("postgresql+asyncpg://user:pass@db.example.com:5432/postgres")
        assert url.drivername == "postgresql+asyncpg"

    def test_direct_connection_has_no_statement_cache_override(self):
        url, connect_args = build_engine_options(
            normalize_database_url("postgresql://user:pass@db.example.com:5432/postgres")
        )
        assert url.port == 5432
        assert "statement_cache_size" not in connect_args

    def test_transaction_pooler_disables_statement_cache(self):
        url, connect_args = build_engine_options(
            normalize_database_url(
                "postgresql://user:pass@pooler.example.com:6543/postgres?sslmode=require"
            )
        )
        assert connect_args["statement_cache_size"] == 0
        assert connect_args["ssl"] is True
        assert "sslmode" not in url.query

    def test_pgbouncer_flag_disables_statement_cache(self):
        _, connect_args = build_engine_options(
            normalize_database_url(
                "postgresql://user:pass@db.example.com:5432/postgres?pgbouncer=true"
            )
        )
        assert connect_args["statement_cache_size"] == 0

    def test_explicit_cache_size_wins(self):
        _, connect_args = build_engine_options(
            normalize_database_url(
                "postgresql://user:pass@db.example.com:6543/postgres?statement_cache_size=50"
            )
        )
        assert connect_args["statement_cache_size"] == 50

    def test_sslmode_disable(self):
        _, connect_args = build_engine_options(
            normalize_database_url(
                "postgresql://user:pass@db.example.com:5432/postgres?sslmode=disable"
            )
        )
        assert connect_args["ssl"] is False
