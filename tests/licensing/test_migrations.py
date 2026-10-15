import logging
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


def test_migration_keeps_unrelated_application_logging_available(tmp_path):
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("fsm_panel.migration_test")
    handler = Capture()
    previous = (logger.level, logger.propagate, logger.disabled)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.disabled = False
    try:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.attributes["database_url"] = (
            "sqlite+pysqlite:///" + (tmp_path / "logging.db").as_posix()
        )
        command.upgrade(config, "head")
        logger.info("application message after migration")
        assert records == ["application message after migration"]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous[0])
        logger.propagate, logger.disabled = previous[1:]
