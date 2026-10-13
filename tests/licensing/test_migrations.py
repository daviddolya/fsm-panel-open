from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from fsm_license.database import Base


def test_initial_migration_matches_models_and_is_repeatable(tmp_path):
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.attributes["database_url"] = (
        "sqlite+pysqlite:///" + (tmp_path / "migration.db").as_posix()
    )
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = create_engine(config.attributes["database_url"])
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        assert {"entitlements", "activations", "proof_nonces"} <= set(
            inspect(connection).get_table_names()
        )
    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert "entitlements" not in inspect(connection).get_table_names()
    engine.dispose()
