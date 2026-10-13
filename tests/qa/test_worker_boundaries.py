import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from fsm_panel.core import AccountRepository
from fsm_panel.trading import (
    NodeWorkerClient,
    SteamCredentials,
    TradeError,
    TradeService,
    TradeState,
)
from fsm_panel.trading.models import InventoryResult, TradeResult

ROOT = Path(__file__).resolve().parents[2]
CREDENTIALS = SteamCredentials("synthetic_qa", "synthetic-password", "synthetic-shared")


def worker(tmp_path, scenario=None):
    node = shutil.which("node")
    assert node is not None, "Node 24 is required for independent IPC checks"
    fixture = tmp_path / "qa-scenario.json"
    fixture.write_text(json.dumps(scenario or {}), encoding="utf-8")
    return NodeWorkerClient(
        [
            node,
            str(ROOT / "worker/src/main.ts"),
            "--state",
            str(tmp_path / "worker.sqlite"),
            "--scenario",
            str(fixture),
        ]
    )


async def test_completion_exemption_cannot_create_an_offer_without_admission(tmp_path):
    with AccountRepository() as repository:
        account = repository.add_account("synthetic_completion_scope")
        async with worker(tmp_path) as client:
            service = TradeService(repository, client)
            with pytest.raises(TradeError, match="operation_not_found"):
                await service.confirm(account.id, "missing-intent", CREDENTIALS)
            with pytest.raises(TradeError, match="admission_required"):
                await service.send(
                    account.id,
                    "new-intent",
                    "https://steamcommunity.com/tradeoffer/new/?partner=123",
                    CREDENTIALS,
                )
            assert not repository.list_effects()


async def test_unknown_offer_without_id_cannot_be_confirmed_or_inferred_absent(tmp_path):
    scenario = {
        "pages": {
            "730/2": [
                {"assets": [{"app_id": 730, "context_id": "2", "asset_id": "31", "amount": 1}]}
            ]
        },
        "send_error": True,
        "reconcile_state": "failed",
    }

    async def admission(account_id):
        return int(time.time() * 1000) + 30_000

    with AccountRepository() as repository:
        account = repository.add_account("synthetic_unknown_completion")
        async with worker(tmp_path, scenario) as client:
            service = TradeService(repository, client, admission)
            result = await service.send(
                account.id,
                "unknown-intent",
                "https://steamcommunity.com/tradeoffer/new/?partner=123",
                CREDENTIALS,
            )
            assert result.state == TradeState.UNKNOWN_OUTCOME
            assert result.offer_id is None
            service.admission = None
            assert (
                await service.confirm(account.id, "unknown-intent", CREDENTIALS)
            ).state == TradeState.UNKNOWN_OUTCOME
            assert (
                await service.reconcile(account.id, "unknown-intent", CREDENTIALS)
            ).state == TradeState.UNKNOWN_OUTCOME


async def test_invalid_destination_is_rejected_before_reserving_external_effect(tmp_path):
    async def admission(account_id):
        return int(time.time() * 1000) + 30_000

    with AccountRepository() as repository:
        account = repository.add_account("synthetic_invalid_destination")
        async with worker(tmp_path) as client:
            service = TradeService(repository, client, admission)
            with pytest.raises(TradeError):
                await service.send(
                    account.id, "invalid-intent", "https://example.invalid/", CREDENTIALS
                )
            # No effect was possible: local input validation must not strand the
            # account in UNKNOWN with no worker operation available to reconcile.
            assert not repository.list_effects()


@pytest.mark.parametrize("kind", ["foreign", "missing_primary_key"])
async def test_worker_rejects_unrecognized_or_constraint_damaged_journal(tmp_path, kind):
    with closing(sqlite3.connect(tmp_path / "worker.sqlite")) as connection:
        if kind == "foreign":
            connection.execute("CREATE TABLE private_foreign_data (id TEXT)")
        else:
            connection.executescript(
                "CREATE TABLE operations (operation_id TEXT, account_id TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, partner_id TEXT NOT NULL, state TEXT NOT NULL, "
                "phase TEXT NOT NULL, offer_id TEXT, code TEXT, updated_at_ms INTEGER NOT NULL, "
                "simulation INTEGER NOT NULL); PRAGMA user_version=1;"
            )
    async with worker(tmp_path) as client:
        with pytest.raises(TradeError, match="worker_disconnected"):
            await client.request("status", {"account_id": "synthetic", "operation_id": "missing"})


def test_inventory_success_payload_rejects_noninteger_asset_amount():
    with pytest.raises(TradeError, match="invalid_worker_response"):
        InventoryResult.from_payload(
            {
                "assets": [{"app_id": 730, "context_id": "2", "asset_id": "31", "amount": True}],
                "failures": [],
                "simulation": False,
            }
        )


def test_trade_success_payload_rejects_arbitrary_diagnostic_code():
    with pytest.raises(TradeError, match="invalid_worker_response"):
        TradeResult.from_payload(
            {
                "operation_id": "synthetic-intent",
                "account_id": "synthetic-account",
                "state": "unknown_outcome",
                "offer_id": None,
                "code": "synthetic credential unexpectedly echoed by a malformed worker",
                "updated_at_ms": 123,
                "simulation": False,
            }
        )
