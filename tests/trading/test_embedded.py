import json
import time

import pytest

from fsm_panel.core import AccountRepository, DuplicateEffect, EffectState
from fsm_panel.trading import Asset, TradeError, TradeService, TradeState, load_selected_assets

from .test_ipc import CREDENTIALS, URL, admit, worker


def test_selected_file_normalizes_legacy_keys_and_rejects_ambiguous_values(tmp_path):
    file = tmp_path / "selected.json"
    file.write_text(
        json.dumps(
            [
                {"appid": "730", "id": "123"},
                {"app_id": 730, "asset_id": "123", "context_id": "2"},
                {"appid": 440, "contextid": "2", "assetid": "456"},
            ]
        )
    )
    assert load_selected_assets(file) == (Asset(730, "2", "123", 1), Asset(440, "2", "456", 1))
    file.write_text('[{"appid": "730garbage", "id": "123"}]')
    with pytest.raises(TradeError, match="invalid_request"):
        load_selected_assets(file)


async def test_grouped_selected_sends_two_durable_children_and_confirms_each(tmp_path):
    with AccountRepository() as repository:
        account = repository.add_account("synthetic")
        async with worker(tmp_path, {"send_status": "pending"}) as client:
            service = TradeService(repository, client, admit)
            results = await service.send_grouped(
                account.id,
                "batch-a",
                URL,
                CREDENTIALS,
                selected_assets=[Asset(730, "2", "123", 1), Asset(440, "2", "456", 1)],
                comment="synthetic",
            )
            assert [result.operation_id for result in results] == ["batch-a:cs2", "batch-a:other"]
            assert all(result.state == TradeState.CONFIRMED for result in results)
            assert len(repository.list_effects()) == 3
            with pytest.raises(DuplicateEffect):
                await service.send_grouped(
                    account.id,
                    "batch-a",
                    URL,
                    CREDENTIALS,
                    selected_assets=[Asset(730, "2", "123", 1), Asset(440, "2", "456", 1)],
                )


async def test_grouped_send_stops_after_unknown_first_child(tmp_path):
    with AccountRepository() as repository:
        account = repository.add_account("synthetic")
        async with worker(tmp_path, {"send_error": True}) as client:
            service = TradeService(repository, client, admit)
            results = await service.send_grouped(
                account.id,
                "batch-a",
                URL,
                CREDENTIALS,
                selected_assets=[Asset(730, "2", "123", 1), Asset(440, "2", "456", 1)],
            )
            assert len(results) == 1 and results[0].state == TradeState.UNKNOWN_OUTCOME
            assert len(repository.list_effects()) == 2


async def test_activity_real_ipc_and_lease_renewal_with_synthetic_clock_delays(tmp_path):
    calls = 0

    async def licence(_account_id):
        nonlocal calls
        calls += 1
        return int(time.time() * 1000) + (450 if calls == 1 else 30_000)

    with AccountRepository() as repository:
        account = repository.add_account("synthetic")
        async with worker(tmp_path, {"activity_delay_ms": 400}) as client:
            service = TradeService(repository, client, licence)
            result = await service.activity(
                account.id, "activity-a", CREDENTIALS, [730, 440, 730], 1
            )
            assert result.state == "completed" and result.granted_app_ids == (730, 440)
            assert result.simulation
            assert repository.list_effects()[0].state == EffectState.SUCCEEDED
            service.admission = None
            assert (await service.activity_status(account.id, "activity-a")).state == "completed"


async def test_short_lease_renews_for_two_consecutive_activities(tmp_path):
    async def short_licence(_account_id):
        return int(time.time() * 1000) + 450

    with AccountRepository() as repository:
        account = repository.add_account("synthetic")
        async with worker(tmp_path, {"activity_delay_ms": 400}) as client:
            service = TradeService(repository, client, short_licence)
            for operation_id in ("short-first", "short-second"):
                result = await service.activity(account.id, operation_id, CREDENTIALS, [730], 1)
                assert result.state == "completed"
