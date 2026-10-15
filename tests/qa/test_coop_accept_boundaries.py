"""Independent checkpoint-03 caller cases; all effects are synthetic data."""

import builtins
import math
import socket
import threading
import time

import pytest

from fsm_panel.adapters.coop_accept_plan import (
    CoopAcceptPlanError,
    CoopAcceptRequest,
    CoopRef,
    ThreadRef,
    iter_legacy_coop_accept,
)


def consume(sequence, respond, seen=None):
    seen = [] if seen is None else seen
    try:
        request = next(sequence)
        while True:
            seen.append(request)
            answer = respond(request)
            request = (
                sequence.throw(answer)
                if isinstance(answer, BaseException)
                else sequence.send(answer)
            )
    except StopIteration as done:
        return seen, done.value


class World:
    """A test-owned observation source and ledger, never an operation executor."""

    def __init__(self):
        self.coop = CoopRef("original")
        self.accounts = {"login": {"steamid": "id"}}
        self.servers = frozenset({"old state"})
        self.consumed_server = "s"
        self.autoaccept = True
        self.enabled = iter((True, True))
        self.minimums = iter((4.0, 4.0))
        self.clock = {
            "ticket_age": 102.25,
            "activation_timestamp": 201,
            "last_accepted_game_timestamp": 202,
        }
        self.regions = None
        self.in_menu = False
        self.accepted = []
        self.settings = {}
        self.completed = []
        self.requests = []
        self.fail_at = None
        self.failure = RuntimeError("synthetic caller dependency failure")

    def respond(self, request):
        self.requests.append(request)
        if len(self.requests) - 1 == self.fail_at:
            return self.failure
        operation, args = request.operation, request.arguments
        if operation == "read_coop":
            return self.coop
        if operation == "read_launched_account":
            return self.accounts.get(args[0])
        if operation == "observe_all_in_menu":
            return self.in_menu
        if operation == "observe_regions":
            return self.regions
        if operation == "short_id":
            return "short:" + args[0]
        if operation == "read_coop_servers":
            return self.servers
        if operation == "read_setting":
            return self.autoaccept if args == ("AUTOACCEPTGAME",) else next(self.enabled)
        if operation == "accepted_games_contains":
            return args[0] in self.accepted
        if operation == "read_global":
            return next(self.minimums)
        if operation == "read_time":
            return self.clock[args[0]]
        if operation == "consume_accept":
            self.completed.append(request)
            return self.consumed_server
        if operation == "construct_thread":
            self.completed.append(request)
            return ThreadRef("constructed")
        if operation == "write_coop_servers":
            self.servers = args[0]
        elif operation == "append_accepted_game":
            self.accepted.append(args[0])
        elif operation == "write_setting":
            self.settings[args[0]] = args[1]
        else:
            assert operation in {
                "coop_log",
                "report_tickets",
                "leave_match",
                "application_log",
                "start_thread",
            }
        self.completed.append(request)
        return None


ROWS = (("login", "s", 100),)


def log_fields(trace, event):
    return dict(
        next(
            item.keywords
            for item in trace
            if item.operation == "coop_log" and item.arguments == (event,)
        )
    )


def test_none_coop_bypasses_even_invalid_ticket_container_and_all_state_changes():
    class Unreadable:
        def __iter__(self):
            pytest.fail("tickets were iterated without co-op")

        def __bool__(self):
            pytest.fail("tickets were truth-tested without co-op")

    world = World()
    world.coop = None
    trace, result = consume(iter_legacy_coop_accept(Unreadable()), world.respond)
    assert trace == [CoopAcceptRequest("read_coop")]
    assert result is None and world.completed == []
    assert world.servers == frozenset({"old state"})


@pytest.mark.parametrize("server", [None, False, 0, -0.0, "", (), [], {}])
def test_falsey_server_skips_login_and_timestamp_validation_before_account_lookup(server):
    world = World()
    world.consumed_server = None
    row = (object(), server, object())
    trace, result = consume(iter_legacy_coop_accept((row,)), world.respond)
    assert result is None
    assert not any(item.operation == "read_launched_account" for item in trace)
    assert world.servers == frozenset()
    assert log_fields(trace, "TICKETS") == {
        "found": 1,
        "sent": 0,
        "in_menu": False,
        "servers": (),
        "regions": "any",
    }
    assert next(item.arguments for item in trace if item.operation == "report_tickets") == (
        world.coop,
        (),
        False,
        None,
    )


@pytest.mark.parametrize(
    "account,expected",
    [
        (None, None),
        (False, None),
        (0, None),
        ("", None),
        ([], None),
        ({}, None),
        ({"other": "value"}, None),
        ({"steamid": ""}, None),
        ({"steamid": None}, "None"),
        ({"steamid": False}, "False"),
        ({"steamid": 0}, "0"),
        ({"steamid": "001"}, "001"),
    ],
)
def test_account_fallback_and_scalar_stringification_preserve_non_numeric_ids(account, expected):
    world = World()
    world.accounts["login"] = account
    world.consumed_server = None
    # An excluded row's malformed timestamp must remain unread.
    trace, _ = consume(
        iter_legacy_coop_accept((("login", "s", object() if expected is None else 0),)),
        world.respond,
    )
    report = next(item for item in trace if item.operation == "report_tickets")
    assert report.arguments[1] == (() if expected is None else ((expected, "s"),))


def test_duplicate_ticket_order_and_short_id_calls_are_not_deduplicated_early():
    world = World()
    world.consumed_server = None
    accounts = iter(({"steamid": "first"}, {"steamid": "second"}, {"steamid": "first"}))
    shortened = iter(("shared", "shared", "other"))
    lookups, short_calls = [], []

    def respond(request):
        if request.operation == "read_launched_account":
            lookups.append(request.arguments)
            return next(accounts)
        if request.operation == "short_id":
            short_calls.append(request.arguments)
            return next(shortened)
        return world.respond(request)

    rows = (("same", "s", 5), ("same", "t", 2), ("same", "s", 3))
    trace, _ = consume(iter_legacy_coop_accept(rows), respond)
    assert lookups == [("same",)] * 3 and short_calls == [("s",), ("t",), ("s",)]
    assert next(item.arguments[1] for item in trace if item.operation == "report_tickets") == (
        ("first", "s"),
        ("second", "t"),
        ("first", "s"),
    )
    assert log_fields(trace, "TICKETS")["servers"] == ("other", "shared")
    assert world.servers == frozenset({"s", "t"})


@pytest.mark.parametrize("regions", [None, False, 0, "", (), ("eu",)])
def test_empty_input_still_observes_menu_and_regions_then_reports_and_consumes(regions):
    world = World()
    world.regions = regions
    world.consumed_server = None
    trace, result = consume(iter_legacy_coop_accept(()), world.respond)
    assert result is None and trace == [
        CoopAcceptRequest("read_coop"),
        CoopAcceptRequest("write_coop_servers", (frozenset(),)),
        CoopAcceptRequest("observe_all_in_menu"),
        CoopAcceptRequest("observe_regions"),
        CoopAcceptRequest("report_tickets", (world.coop, (), False, regions)),
        CoopAcceptRequest("consume_accept", (world.coop,)),
    ]


@pytest.mark.parametrize("regions", [None, False, 0, "", (), ("eu",)])
def test_nonempty_input_log_fallback_does_not_change_report_regions(regions):
    world = World()
    world.regions = regions
    world.consumed_server = None
    trace, _ = consume(iter_legacy_coop_accept(ROWS), world.respond)
    assert log_fields(trace, "TICKETS")["regions"] == (regions or "any")
    assert (
        next(item.arguments[3] for item in trace if item.operation == "report_tickets") == regions
    )


@pytest.mark.parametrize("gate", ["none", "disabled", "already accepted"])
def test_report_and_consume_precede_short_circuit_gates(gate):
    world = World()
    if gate == "none":
        world.consumed_server = None
    elif gate == "disabled":
        world.autoaccept = False
    else:
        world.accepted = ["s"]
    trace, result = consume(iter_legacy_coop_accept(ROWS), world.respond)
    operations = [item.operation for item in trace]
    report, consume_at = operations.index("report_tickets"), operations.index("consume_accept")
    assert report + 1 == consume_at and result is None
    expected_tail = [] if gate == "none" else ["read_setting"]
    if gate == "already accepted":
        expected_tail.append("accepted_games_contains")
    assert operations[consume_at + 1 :] == expected_tail
    assert world.settings == {} and world.accepted == (["s"] if gate == "already accepted" else [])


@pytest.mark.parametrize("coop_token", ["", "original"])
def test_empty_consumed_server_is_not_none_and_uses_captured_coop_identity(coop_token):
    world = World()
    world.coop = CoopRef(coop_token)
    original = world.coop
    world.consumed_server = ""

    def respond(request):
        if request.operation == "report_tickets":
            answer = world.respond(request)
            world.coop = CoopRef("changed after report")
            return answer
        if request.operation == "read_coop_servers":
            return frozenset()
        return world.respond(request)

    trace, _ = consume(iter_legacy_coop_accept(ROWS), respond)
    assert sum(item.operation == "read_coop" for item in trace) == 1
    assert next(item.arguments for item in trace if item.operation == "consume_accept") == (
        original,
    )
    assert next(
        item.arguments for item in trace if item.operation == "accepted_games_contains"
    ) == ("",)
    assert trace[-1] == CoopAcceptRequest("leave_match", (original, "match_cancelled"))


def test_refusal_uses_whole_set_and_logs_fresh_duplicate_short_ids_before_leave():
    world = World()
    sets = iter((frozenset({"s", "other"}), frozenset({"fresh1", "fresh2"})))

    def respond(request):
        if request.operation == "read_coop_servers":
            return next(sets)
        if request.operation == "short_id":
            return "requested" if request.arguments == ("s",) else "same"
        return world.respond(request)

    trace, _ = consume(iter_legacy_coop_accept(ROWS), respond)
    assert log_fields(trace, "ACCEPT_REFUSED") == {
        "server": "requested",
        "current": ("same", "same"),
    }
    assert trace[-2].operation == "coop_log" and trace[-2].arguments == ("ACCEPT_REFUSED",)
    assert trace[-1] == CoopAcceptRequest("leave_match", (world.coop, "match_cancelled"))
    assert world.accepted == [] and world.settings == {}
    gate = next(index for index, item in enumerate(trace) if item.operation == "read_coop_servers")
    assert trace[gate + 1 : gate + 3] == [
        CoopAcceptRequest("short_id", ("s",)),
        CoopAcceptRequest("read_coop_servers"),
    ]


@pytest.mark.parametrize(
    "timestamps,now,expected_age,expected_delay,age_reads",
    [
        ((-10, -1), 500, 0.0, 4.0, 0),
        ((None, False, 0, "", (), [], {}), 500, 0.0, 4.0, 0),
        ((100, 90, 99), 102.25, 2.25, 1.75, 1),
        ((100, 90, 105), 102, -3, 4.0, 1),
        ((100, 100), 104, 4, 0.0, 1),
        ((100,), 500, 400, 0.0, 1),
    ],
)
def test_seen_at_uses_maximum_with_zero_floor_and_only_reads_age_clock_when_truthy(
    timestamps, now, expected_age, expected_delay, age_reads
):
    world = World()
    world.clock["ticket_age"] = now
    rows = tuple(("login", "s", timestamp) for timestamp in timestamps)
    trace, _ = consume(iter_legacy_coop_accept(rows), world.respond)
    accepting = log_fields(trace, "ACCEPTING")
    assert accepting["age"] == round(expected_age, 1) and accepting["wait"] == round(
        expected_delay, 1
    )
    if not age_reads:
        assert type(accepting["age"]) is float and math.copysign(1, accepting["age"]) == 1
    assert trace.count(CoopAcceptRequest("read_time", ("ticket_age",))) == age_reads
    constructor = next(item for item in trace if item.operation == "construct_thread")
    assert dict(constructor.keywords) == {
        "target": "self.VIXswEwxFlBY",
        "args": (expected_delay, "s"),
        "daemon": True,
    }


@pytest.mark.parametrize(
    "outer,inner,expected", [(2, 8, 2), (8, 2, 0.0), (-2, 8, -2), (8, -2, 0.0)]
)
def test_delay_globals_are_two_ordered_late_reads_and_do_not_use_definition_default(
    outer, inner, expected
):
    world = World()
    world.clock["ticket_age"] = 103
    world.minimums = iter((outer, inner))
    trace, _ = consume(iter_legacy_coop_accept(ROWS), world.respond)
    global_reads = [index for index, item in enumerate(trace) if item.operation == "read_global"]
    assert len(global_reads) == 2 and global_reads[1] == global_reads[0] + 1
    assert trace[global_reads[0] - 1] == CoopAcceptRequest("read_time", ("ticket_age",))
    assert trace[global_reads[1] + 1] == CoopAcceptRequest("append_accepted_game", ("s",))
    constructor = next(item for item in trace if item.operation == "construct_thread")
    assert dict(constructor.keywords)["args"] == (expected, "s")


@pytest.mark.parametrize(
    "logged_enabled,gate_enabled", [(False, False), (False, True), (True, False), (True, True)]
)
def test_enable_is_reread_after_log_and_disabled_gate_keeps_consumption_and_state(
    logged_enabled, gate_enabled
):
    world = World()
    world.enabled = iter((logged_enabled, gate_enabled))
    trace, _ = consume(iter_legacy_coop_accept(ROWS), world.respond)
    assert world.accepted == ["s"]
    assert world.settings == {"LAST_AUTOACCEPTER_ACTIVATION": 201, "last_accepted_game": 202}
    assert log_fields(trace, "ACCEPTING")["enabled"] is logged_enabled
    first, second = [
        index
        for index, item in enumerate(trace)
        if item == CoopAcceptRequest("read_setting", ("ENABLE_AUTOACCEPT",))
    ]
    assert trace[first + 1].operation == "coop_log" and first + 2 == second
    assert [item.operation for item in trace[second + 1 :]] == (
        ["application_log", "construct_thread", "start_thread"] if gate_enabled else []
    )
    assert trace.count(CoopAcceptRequest("consume_accept", (world.coop,))) == 1
    assert not any(item.arguments == ("ALTERNATIVE_AUTOACCEPTER",) for item in trace)


@pytest.mark.parametrize("setting", ["false", "0", (False,), 1])
def test_truthy_builtin_flags_and_false_in_menu_still_reach_thread_request(setting):
    world = World()
    world.autoaccept = setting
    world.enabled = iter((setting, setting))
    world.in_menu = False
    trace, _ = consume(iter_legacy_coop_accept(ROWS), world.respond)
    assert log_fields(trace, "ACCEPTING")["enabled"] == setting
    assert trace[-1] == CoopAcceptRequest("start_thread", (ThreadRef("constructed"),))
    assert world.accepted == ["s"]


def test_accepted_marker_suppresses_only_later_dispatch_not_a_second_report_or_consume():
    world = World()
    first, _ = consume(iter_legacy_coop_accept(ROWS), world.respond)
    second, _ = consume(iter_legacy_coop_accept(ROWS), world.respond)
    assert world.accepted == ["s"]
    assert sum(item.operation == "start_thread" for item in first) == 1
    assert second[-1] == CoopAcceptRequest("accepted_games_contains", ("s",))
    assert sum(item.operation == "consume_accept" for item in world.completed) == 2
    assert sum(item.operation == "report_tickets" for item in world.completed) == 2


@pytest.mark.parametrize("refuse", [False, True])
def test_exception_at_each_request_keeps_exact_completed_state_prefix_without_retry_or_cleanup(
    refuse,
):
    baseline_world = World()
    if refuse:
        baseline_world.consumed_server = "different"
    baseline, _ = consume(iter_legacy_coop_accept(ROWS), baseline_world.respond)
    effects = baseline_world.completed
    for failed in range(len(baseline)):
        world = World()
        world.fail_at = failed
        if refuse:
            world.consumed_server = "different"
        sequence = iter_legacy_coop_accept(ROWS)
        with pytest.raises(RuntimeError) as caught:
            consume(sequence, world.respond)
        assert caught.value is world.failure
        assert world.requests == baseline[: failed + 1]
        acknowledged = [item for item in baseline[:failed] if item in effects]
        assert world.completed == acknowledged
        assert world.accepted == (
            ["s"] if CoopAcceptRequest("append_accepted_game", ("s",)) in acknowledged else []
        )
        assert world.settings == {
            item.arguments[0]: item.arguments[1]
            for item in acknowledged
            if item.operation == "write_setting"
        }
        assert world.servers == (
            frozenset({"s"})
            if any(item.operation == "write_coop_servers" for item in acknowledged)
            else frozenset({"old state"})
        )
        assert list(sequence) == []


def test_mutation_before_dependency_failure_is_not_rolled_back():
    world = World()

    def respond(request):
        answer = world.respond(request)
        return world.failure if request.operation == "append_accepted_game" else answer

    with pytest.raises(RuntimeError):
        consume(iter_legacy_coop_accept(ROWS), respond)
    assert world.accepted == ["s"] and world.settings == {}
    assert world.requests[-1] == CoopAcceptRequest("append_accepted_game", ("s",))


def test_close_at_every_yield_runs_no_real_dependency_and_adds_no_cleanup(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("an inert trace called a real dependency")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(time, "time", forbidden)
        patch.setattr(time, "sleep", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(threading.Thread, "__init__", forbidden)
        patch.setattr(threading.Thread, "start", forbidden)
        baseline, _ = consume(iter_legacy_coop_accept(ROWS), World().respond)
        for stop in range(len(baseline)):
            world = World()
            sequence = iter_legacy_coop_accept(ROWS)
            request = next(sequence)
            for _ in range(stop):
                request = sequence.send(world.respond(request))
            completed = tuple(world.completed)
            sequence.close()
            assert tuple(world.completed) == completed and list(sequence) == []


def test_protocol_error_at_late_write_is_terminal_and_content_free():
    world = World()

    def respond(request):
        if request.operation == "write_setting" and request.arguments[0] == "last_accepted_game":
            return "private invalid acknowledgement"
        return world.respond(request)

    sequence = iter_legacy_coop_accept(ROWS)
    with pytest.raises(CoopAcceptPlanError, match="^coop_accept_plan_response_invalid$") as caught:
        consume(sequence, respond)
    assert "private" not in repr(caught.value)
    assert world.accepted == ["s"] and world.settings == {"LAST_AUTOACCEPTER_ACTIVATION": 201}
    assert list(sequence) == []
