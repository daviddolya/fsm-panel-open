"""Reuse only synthetic service setup, including the isolated PostgreSQL schema fixture."""

from licensing.conftest import service as service
from licensing.test_postgres import pg_service as pg_service
