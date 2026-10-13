import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from fsm_panel.adapters.fakes import FakeClock
from fsm_panel.gsi import GameStateReceiver, GsiError

STEAM_ID = "76561198000000001"
OTHER_STEAM_ID = "76561198000000002"
TOKEN = "synthetic-account-a-token-" + "a" * 32
OTHER_TOKEN = "synthetic-account-b-token-" + "b" * 32


def payload(steam_id=STEAM_ID, token=TOKEN, **extra):
    return json.dumps(
        {"provider": {"steamid": steam_id}, "auth": {"token": token}, **extra}
    ).encode()


@pytest.fixture
def receiver():
    result = GameStateReceiver(clock=FakeClock(10))
    result.register("account-a", STEAM_ID, TOKEN)
    return result


def test_latest_arrival_has_local_time_and_independent_redacted_copies():
    clock = FakeClock(12)
    receiver = GameStateReceiver(clock=clock)
    receiver.register("account-a", STEAM_ID, TOKEN)
    assert receiver.snapshot("account-a") is None
    first = receiver.receive(
        payload(
            map={"name": "synthetic-map", "nested": [{"Auth": {"token": TOKEN}}]},
            token=TOKEN,
            previously={"token": TOKEN, "round": 9},
        )
    )
    assert first.account_id == "account-a"
    assert first.steam_id == STEAM_ID
    assert first.received_at == 12
    assert first.payload == {
        "provider": {"steamid": STEAM_ID},
        "map": {"name": "synthetic-map", "nested": [{}]},
        "previously": {"round": 9},
    }
    assert TOKEN not in repr(first)
    first.payload["map"]["name"] = "caller-mutated"
    snapshot = receiver.snapshot("account-a")
    assert snapshot.payload["map"]["name"] == "synthetic-map"
    snapshot.payload["map"]["nested"].append("caller-mutated")
    assert receiver.snapshot("account-a").payload["map"]["nested"] == [{}]
    clock.advance(5)
    # Provider time is untrusted telemetry, not a freshness or game-action rule.
    second = receiver.receive(payload(provider_timestamp=-1000, round=2))
    assert second.received_at == 17
    assert receiver.snapshot("account-a").payload["round"] == 2
    assert "map" not in second.payload


def test_accounts_are_isolated_and_cannot_forge_other_registered_steam_id(receiver):
    receiver.register("account-b", OTHER_STEAM_ID, OTHER_TOKEN)
    receiver.receive(payload(round=1))
    receiver.receive(payload(OTHER_STEAM_ID, OTHER_TOKEN, round=2))
    with pytest.raises(GsiError, match="^unauthorized$"):
        receiver.receive(payload(OTHER_STEAM_ID, TOKEN, round=99))
    assert receiver.snapshot("account-a").payload["round"] == 1
    assert receiver.snapshot("account-b").payload["round"] == 2


def test_unregister_revokes_old_token_and_discards_state(receiver):
    receiver.receive(payload())
    receiver.unregister("account-a")
    receiver.unregister("account-a")
    assert receiver.snapshot("account-a") is None
    with pytest.raises(GsiError, match="^unauthorized$"):
        receiver.receive(payload())
    receiver.register("account-a", STEAM_ID, OTHER_TOKEN)
    with pytest.raises(GsiError, match="^unauthorized$"):
        receiver.receive(payload())
    assert receiver.receive(payload(token=OTHER_TOKEN)).account_id == "account-a"


def test_50_registered_accounts_have_independent_snapshots_and_a_hard_limit():
    receiver = GameStateReceiver()
    for index in range(50):
        steam_id, token = str(76561198000000100 + index), f"synthetic-token-{index:032}"
        receiver.register(f"account-{index}", steam_id, token)
        receiver.receive(payload(steam_id, token, round=index))
    assert [receiver.snapshot(f"account-{i}").payload["round"] for i in range(50)] == list(
        range(50)
    )
    with pytest.raises(GsiError, match="^client_limit_reached$"):
        receiver.register("overflow", STEAM_ID, TOKEN)
    receiver.unregister("account-0")
    receiver.register("replacement", STEAM_ID, TOKEN)
    assert receiver.receive(payload()).account_id == "replacement"
    with pytest.raises(ValueError, match="invalid_max_clients"):
        GameStateReceiver(max_clients=51)


@pytest.mark.parametrize(
    ("account_id", "steam_id", "token", "error"),
    [
        ("account-a", OTHER_STEAM_ID, OTHER_TOKEN, "registration_exists"),
        ("account-b", STEAM_ID, OTHER_TOKEN, "registration_exists"),
        ("account-b", OTHER_STEAM_ID, TOKEN, "token_already_registered"),
        ("account-b", OTHER_STEAM_ID, "short", "invalid_token"),
        ("account-b", OTHER_STEAM_ID, "я" * 40, "invalid_token"),
        ("account-b", OTHER_STEAM_ID, " " * 40, "invalid_token"),
        ("", OTHER_STEAM_ID, OTHER_TOKEN, "invalid_account_id"),
    ],
)
def test_invalid_registration_does_not_change_existing_registration(
    receiver, account_id, steam_id, token, error
):
    with pytest.raises(GsiError, match=f"^{error}$"):
        receiver.register(account_id, steam_id, token)
    assert receiver.receive(payload()).account_id == "account-a"


@pytest.mark.parametrize("steam_id", [None, True, 123, "-1", "01", "1.0", "１", "0", str(1 << 64)])
def test_steam_id_requires_unambiguous_unsigned_decimal_string(receiver, steam_id):
    with pytest.raises(GsiError, match="^invalid_steam_id$"):
        receiver.receive(payload(steam_id))
    assert receiver.snapshot("account-a") is None


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b"{",
        b"null",
        b"[]",
        b"{}",
        b'{"provider": []}',
        b'{"provider": {}, "provider": {}}',
        payload().replace(b'"provider":', b'"bad": NaN, "provider":'),
        payload(value=1e300).replace(b"1e+300", b"1e999"),
    ],
)
def test_malformed_input_is_safe_and_does_not_overwrite(receiver, body):
    previous = receiver.receive(payload(round=3))
    with pytest.raises(GsiError, match="^invalid_payload$"):
        receiver.receive(body)
    assert receiver.snapshot("account-a") == previous


def test_unknown_account_and_wrong_token_have_same_failure(receiver):
    for body in (payload(OTHER_STEAM_ID), payload(token=OTHER_TOKEN), payload(token=None)):
        with pytest.raises(GsiError) as error:
            receiver.receive(body)
        assert (error.value.code, error.value.status_code) == ("unauthorized", 401)
    assert receiver.snapshot("account-a") is None


def test_payload_size_and_depth_are_bounded_before_decoding():
    receiver = GameStateReceiver(max_payload_bytes=512, max_depth=4)
    receiver.register("account-a", STEAM_ID, TOKEN)
    with pytest.raises(GsiError, match="^payload_too_large$"):
        receiver.receive(b"\xff" * 513)
    with pytest.raises(GsiError, match="^payload_too_deep$"):
        receiver.receive(payload(extra=[[[[0]]]]))
    snapshot = receiver.receive(payload(extra='[{{"\\' * 20))
    assert snapshot.payload["extra"].startswith("[{")


def test_clock_failure_does_not_leak_or_replace_state():
    class BrokenClock(FakeClock):
        def monotonic(self):
            raise RuntimeError(TOKEN)

    receiver = GameStateReceiver(clock=BrokenClock())
    receiver.register("account-a", STEAM_ID, TOKEN)
    with pytest.raises(GsiError, match="^clock_unavailable$") as error:
        receiver.receive(payload())
    assert TOKEN not in str(error.value)
    assert receiver.snapshot("account-a") is None


def test_concurrent_registration_cannot_exceed_the_client_limit():
    receiver = GameStateReceiver(max_clients=5)

    def register(index):
        try:
            receiver.register(str(index), str(1000 + index), f"synthetic-token-{index:032}")
        except GsiError as error:
            assert error.code == "client_limit_reached"
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(register, range(40)))
    assert sum(results) == 5
