import asyncio
import inspect
import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from fsm_panel.core import AccountRepository, DuplicateEffect, EffectState
from fsm_panel.trading import (
    Asset,
    NodeWorkerClient,
    SteamCredentials,
    TradeService,
    TradeState,
)

CREDENTIALS = SteamCredentials(
    "synthetic-qa-embedded", "synthetic-password", "synthetic-shared", "synthetic-identity"
)
DESTINATION = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=synthetic"


async def _admit(_account_id):
    return int(time.time() * 1000) + 30_000


def _worker(tmp_path, scenario):
    node = shutil.which("node")
    assert node, "Node 24 is required for this actual worker IPC regression"
    root = Path(inspect.getfile(TradeService)).resolve().parents[3]
    fixture = tmp_path / "synthetic-embedded.json"
    fixture.write_text(json.dumps(scenario), encoding="utf8")
    return NodeWorkerClient(
        [
            node,
            str(root / "worker/src/main.ts"),
            "--state",
            str(tmp_path / "worker.sqlite"),
            "--scenario",
            str(fixture),
        ]
    )


async def _wait_activity_state(path, operation_id, expected):
    async with asyncio.timeout(3):
        while True:
            if path.exists():
                with sqlite3.connect(path) as connection:
                    table = connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='activities'"
                    ).fetchone()
                    if table:
                        row = connection.execute(
                            "SELECT state FROM activities WHERE operation_id=?", (operation_id,)
                        ).fetchone()
                        if row and row[0] == expected:
                            return
            await asyncio.sleep(0.01)


async def test_authoritative_cancelled_activity_can_release_core_unknown_after_restart(tmp_path):
    core_path = tmp_path / "core.sqlite"
    with AccountRepository(core_path) as repository:
        account = repository.add_account("synthetic_activity_restart")
        async with _worker(tmp_path, {"activity_delay_ms": 10_000}) as worker:
            service = TradeService(repository, worker, _admit)
            task = asyncio.create_task(
                service.activity(account.id, "first-activity", CREDENTIALS, [730], 1)
            )
            await _wait_activity_state(tmp_path / "worker.sqlite", "first-activity", "warming_up")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert repository.list_effects()[0].state == EffectState.UNKNOWN

    with AccountRepository(core_path) as repository:
        async with _worker(tmp_path, {"activity_delay_ms": 0}) as worker:
            service = TradeService(repository, worker, _admit)
            status = await service.activity_status(account.id, "first-activity")
            assert status.state == "cancelled"
            assert repository.list_effects()[0].state == EffectState.FAILED_CONFIRMED
            result = await service.activity(account.id, "next-activity", CREDENTIALS, [730], 1)
            assert result.state == "completed"


async def test_batch_id_cannot_be_reused_to_dispatch_a_different_group_after_restart(tmp_path):
    core_path = tmp_path / "core.sqlite"
    with AccountRepository(core_path) as repository:
        account = repository.add_account("synthetic_batch_restart")
        async with _worker(tmp_path, {}) as worker:
            service = TradeService(repository, worker, _admit)
            first = await service.send_grouped(
                account.id,
                "same-batch",
                DESTINATION,
                CREDENTIALS,
                selected_assets=[Asset(730, "2", "1001", 1)],
            )
            assert len(first) == 1 and first[0].state == TradeState.SENT
            previous_effect_ids = {effect.id for effect in repository.list_effects()}

    with AccountRepository(core_path) as repository:
        async with _worker(tmp_path, {}) as worker:
            service = TradeService(repository, worker, _admit)
            with pytest.raises(DuplicateEffect):
                await service.send_grouped(
                    account.id,
                    "same-batch",
                    DESTINATION,
                    CREDENTIALS,
                    selected_assets=[Asset(440, "2", "1002", 1)],
                )
            assert {effect.id for effect in repository.list_effects()} == previous_effect_ids
