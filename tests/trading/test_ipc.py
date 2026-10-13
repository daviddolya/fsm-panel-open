import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from fsm_panel.core import AccountRepository, DuplicateEffect, EffectState
from fsm_panel.trading import (
    InventorySpec,
    NodeWorkerClient,
    SteamCredentials,
    TradeError,
    TradeService,
    TradeState,
)

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
CREDENTIALS = SteamCredentials("synthetic_account", "synthetic_password_secret",
                               "synthetic_shared_secret", "synthetic_identity_secret")
URL = "https://steamcommunity.com/tradeoffer/new/?partner=123&token=synthetic"
ASSET = {"app_id": 730, "context_id": "2", "asset_id": "1001", "amount": 1}


async def admit(_account_id):
    return int(time.time() * 1000) + 30_000


def worker(tmp_path, scenario=None, state=None):
    if not NODE:
        pytest.fail("Node 24 is required for the real Steam worker IPC test")
    fixture = tmp_path / "synthetic.json"
    fixture.write_text(json.dumps(scenario or {}), encoding="utf8")
    return NodeWorkerClient([NODE, str(ROOT / "worker/src/main.ts"), "--state",
                             str(state or tmp_path / "trade.sqlite"), "--scenario", str(fixture)])


@pytest.mark.asyncio
async def test_real_node_process_inventory_send_pending_confirm_status(tmp_path):
    scenario = {"pages": {"730/2": [{"assets": [ASSET]}, {"assets": [ASSET]}],
                          "440/2": [{"error": True}]}, "send_status": "pending"}
    with AccountRepository(tmp_path / "core.sqlite") as repo:
        account = repo.add_account("synthetic")
        async with worker(tmp_path, scenario) as client:
            service = TradeService(repo, client, admit)
            inventory = await service.inventory(account.id, CREDENTIALS,
                                                [InventorySpec(), InventorySpec(440)])
            assert inventory.simulation is True
            assert len(inventory.assets) == 1
            assert inventory.failures[0].code == "inventory_failed"
            result = await service.send(account.id, "intent-a", URL, CREDENTIALS)
            assert result.state == TradeState.CREATED_PENDING_CONFIRMATION
            assert repo.list_effects()[0].state == EffectState.SUCCEEDED
            service.admission = None
            confirmed = await service.confirm(account.id, "intent-a", CREDENTIALS)
            assert confirmed.state == TradeState.CONFIRMED
            assert (await service.status(account.id, "intent-a")).offer_id == "900000001"
            assert "synthetic_password_secret" not in repr(CREDENTIALS)
            assert all("secret" not in arg for arg in client.command)
    raw = (tmp_path / "trade.sqlite").read_bytes()
    for secret in CREDENTIALS.payload().values():
        assert secret.encode() not in raw


@pytest.mark.asyncio
async def test_lost_response_restart_never_resends_and_unknown_reconcile_stays_unknown(tmp_path):
    state = tmp_path / "trade.sqlite"
    with AccountRepository(tmp_path / "core.sqlite") as repo:
        account = repo.add_account("synthetic")
        scenario = {"pages": {"730/2": [{"assets": [ASSET]}]}, "send_error": True}
        async with worker(tmp_path, scenario, state) as client:
            service = TradeService(repo, client, admit)
            assert (await service.send(account.id, "intent-a", URL, CREDENTIALS)).state \
                == TradeState.UNKNOWN_OUTCOME
        assert repo.list_effects()[0].state == EffectState.UNKNOWN
        async with worker(tmp_path, {"pages": {"730/2": [{"assets": [ASSET]}]}}, state) as client:
            service = TradeService(repo, client, admit)
            with pytest.raises(DuplicateEffect):
                await service.send(account.id, "intent-a", URL, CREDENTIALS)
            assert (await service.reconcile(account.id, "intent-a", CREDENTIALS)).state \
                == TradeState.UNKNOWN_OUTCOME
            # Even bypassing Python's ledger cannot resend the worker operation.
            response = await client.request("send", {"account_id": account.id,
                "operation_id": "intent-a", "trade_url": URL,
                "credentials": CREDENTIALS.payload(), "inventories": [InventorySpec().payload()]},
                admitted_until_ms=await admit(account.id))
            assert response["state"] == "unknown_outcome"


@pytest.mark.asyncio
async def test_default_admission_denies_before_worker_is_started(tmp_path):
    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        client = worker(tmp_path)
        service = TradeService(repo, client)
        with pytest.raises(TradeError, match="admission_required"):
            await service.inventory(account.id, CREDENTIALS)
        with pytest.raises(TradeError, match="admission_required"):
            await service.send(account.id, "intent-a", URL, CREDENTIALS)
        assert client._process is None
        assert not repo.list_effects()


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_marks_ledger_unknown_and_is_not_repeatable(tmp_path):
    scenario = {"pages": {"730/2": [{"assets": [ASSET]}]}, "send_delay_ms": 10_000}
    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        async with worker(tmp_path, scenario) as client:
            service = TradeService(repo, client, admit)
            task = asyncio.create_task(service.send(account.id, "intent-a", URL, CREDENTIALS))
            # Allow the source worker to start and cross the persisted send boundary.
            await asyncio.sleep(0.5)
            task.cancel()
            await asyncio.sleep(0.001)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert repo.list_effects()[0].state == EffectState.UNKNOWN
            assert (await service.status(account.id, "intent-a")).state == TradeState.UNKNOWN_OUTCOME
            with pytest.raises(DuplicateEffect):
                await service.send(account.id, "intent-a", URL, CREDENTIALS)


@pytest.mark.asyncio
async def test_lease_revocation_cancels_active_send_and_recovery_still_works(tmp_path):
    scenario = {"pages": {"730/2": [{"assets": [ASSET]}]}, "send_delay_ms": 10_000}
    valid = True

    async def lease(account_id):
        if not valid:
            raise RuntimeError("private-license-detail")
        return await admit(account_id)

    with AccountRepository() as repo:
        account = repo.add_account("synthetic")
        async with worker(tmp_path, scenario) as client:
            service = TradeService(repo, client, lease)
            task = asyncio.create_task(service.send(account.id, "intent-a", URL, CREDENTIALS))
            await asyncio.sleep(0.5)
            valid = False
            with pytest.raises(TradeError, match="admission_required"):
                await task
            assert repo.list_effects()[0].state == EffectState.UNKNOWN
            assert (await service.status(account.id, "intent-a")).state == TradeState.UNKNOWN_OUTCOME


@pytest.mark.asyncio
async def test_malformed_worker_output_and_stderr_never_leak(tmp_path):
    script = tmp_path / "bad_worker.py"
    script.write_text("import sys\nsys.stderr.write('synthetic_password_secret\\n')\n"
                      "sys.stdin.readline()\nprint('synthetic_password_secret', flush=True)\n")
    async with NodeWorkerClient([sys.executable, str(script)]) as client:
        with pytest.raises(TradeError, match="invalid_worker_response") as caught:
            await client.request("status", {"account_id": "x", "operation_id": "x"})
        assert "synthetic_password_secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_hung_worker_cancel_has_bounded_cleanup_without_recursive_cancel(tmp_path):
    script = tmp_path / "hung_worker.py"
    script.write_text("import time\ntime.sleep(60)\n")
    client = NodeWorkerClient([sys.executable, str(script)])
    started = time.monotonic()
    try:
        task = asyncio.create_task(client.request("status", {"account_id": "x", "operation_id": "x"}))
        await asyncio.sleep(0.1)
        task.cancel()
        for _ in range(3):
            await asyncio.sleep(0.05)
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=8)
        assert time.monotonic() - started < 8
        assert client._process is not None and client._process.returncode is not None
    finally:
        await client.close()
