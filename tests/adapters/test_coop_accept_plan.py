from dataclasses import FrozenInstanceError

import pytest

from fsm_panel.adapters.coop_accept_plan import (
    LEGACY_DISPATCH_TARGET,
    LEGACY_INITIAL_MIN_ACCEPT_DELAY,
    CoopAcceptPlanError,
    CoopAcceptRequest,
    CoopRef,
    ThreadRef,
    iter_legacy_coop_accept,
)


def drive(sequence, respond):
    trace: list[CoopAcceptRequest] = []
    try:
        step = next(sequence)
    except StopIteration as done:
        return trace, done.value
    while True:
        trace.append(step)
        response = respond(step)
        try:
            step = (
                sequence.throw(response)
                if isinstance(response, BaseException)
                else sequence.send(response)
            )
        except StopIteration as done:
            return trace, done.value


class SyntheticState:
    def __init__(self):
        self.coop = CoopRef("captured")
        self.accounts: dict[str, object] = {"one": {"steamid": "id1"}, "two": {"steamid": "id2"}}
        self.server: str | None = "s"
        self.servers: frozenset[str] = frozenset()
        self.server_reads = None
        self.in_menu = False
        self.regions = None
        self.autoaccept = True
        self.enabled = iter((True, True))
        self.minimums = iter((LEGACY_INITIAL_MIN_ACCEPT_DELAY,) * 2)
        self.clocks: dict[str, int | float] = {
            "ticket_age": 101,
            "activation_timestamp": 1000,
            "last_accepted_game_timestamp": 1001,
        }
        self.short_ids = {"s": "S", "other": "O"}
        self.accepted = []
        self.settings = {}
        self.observed = []
        self.completed = []
        self.fail_index = None

    def respond(self, step):
        self.observed.append(step)
        if len(self.observed) - 1 == self.fail_index:
            return RuntimeError("synthetic_dependency_failure")
        op, args = step.operation, step.arguments
        if op == "read_coop":
            return self.coop
        if op == "read_launched_account":
            return self.accounts.get(args[0])
        if op == "observe_all_in_menu":
            return self.in_menu
        if op == "observe_regions":
            return self.regions
        if op == "consume_accept":
            return self.server
        if op == "read_setting":
            return self.autoaccept if args[0] == "AUTOACCEPTGAME" else next(self.enabled)
        if op == "accepted_games_contains":
            return args[0] in self.accepted
        if op == "read_coop_servers":
            return self.servers if self.server_reads is None else next(self.server_reads)
        if op == "short_id":
            return self.short_ids[args[0]]
        if op == "read_global":
            return next(self.minimums)
        if op == "read_time":
            return self.clocks[args[0]]
        if op == "construct_thread":
            self.completed.append(step)
            return ThreadRef("synthetic-thread")
        if op == "write_coop_servers":
            self.servers = args[0]
        elif op == "append_accepted_game":
            self.accepted.append(args[0])
        elif op == "write_setting":
            self.settings[args[0]] = args[1]
        elif op not in (
            "coop_log",
            "report_tickets",
            "leave_match",
            "application_log",
            "start_thread",
        ):
            raise AssertionError(f"Unhandled request: {op}")
        self.completed.append(step)
        return None


ROWS = (("one", "s", 100),)


def log_fields(trace, event):
    return dict(
        next(
            step.keywords
            for step in trace
            if step.operation == "coop_log" and step.arguments == (event,)
        )
    )


def test_success_trace_preserves_full_order_explicit_thread_arguments_and_separate_clocks():
    state = SyntheticState()
    trace, result = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert result is None
    assert trace == [
        CoopAcceptRequest("read_coop"),
        CoopAcceptRequest("read_launched_account", ("one",)),
        CoopAcceptRequest("write_coop_servers", (frozenset(("s",)),)),
        CoopAcceptRequest("observe_all_in_menu"),
        CoopAcceptRequest("observe_regions"),
        CoopAcceptRequest("short_id", ("s",)),
        CoopAcceptRequest(
            "coop_log",
            ("TICKETS",),
            (
                ("found", 1),
                ("sent", 1),
                ("in_menu", False),
                ("servers", ("S",)),
                ("regions", "any"),
            ),
        ),
        CoopAcceptRequest("report_tickets", (state.coop, (("id1", "s"),), False, None)),
        CoopAcceptRequest("consume_accept", (state.coop,)),
        CoopAcceptRequest("read_setting", ("AUTOACCEPTGAME",)),
        CoopAcceptRequest("accepted_games_contains", ("s",)),
        CoopAcceptRequest("read_coop_servers"),
        CoopAcceptRequest("read_time", ("ticket_age",)),
        CoopAcceptRequest("read_global", ("MIN_ACCEPT_DELAY",)),
        CoopAcceptRequest("read_global", ("MIN_ACCEPT_DELAY",)),
        CoopAcceptRequest("append_accepted_game", ("s",)),
        CoopAcceptRequest("read_time", ("activation_timestamp",)),
        CoopAcceptRequest("write_setting", ("LAST_AUTOACCEPTER_ACTIVATION", 1000)),
        CoopAcceptRequest("read_time", ("last_accepted_game_timestamp",)),
        CoopAcceptRequest("write_setting", ("last_accepted_game", 1001)),
        CoopAcceptRequest("short_id", ("s",)),
        CoopAcceptRequest("read_setting", ("ENABLE_AUTOACCEPT",)),
        CoopAcceptRequest(
            "coop_log",
            ("ACCEPTING",),
            (
                ("server", "S"),
                ("enabled", True),
                ("age", 1),
                ("wait", 3.0),
            ),
        ),
        CoopAcceptRequest("read_setting", ("ENABLE_AUTOACCEPT",)),
        CoopAcceptRequest(
            "application_log", ("[A.Accepter] Co-op match confirmed by server. Accepting...",)
        ),
        CoopAcceptRequest(
            "construct_thread",
            keywords=(
                ("target", LEGACY_DISPATCH_TARGET),
                ("args", (3.0, "s")),
                ("daemon", True),
            ),
        ),
        CoopAcceptRequest("start_thread", (ThreadRef("synthetic-thread"),)),
    ]
    assert state.accepted == ["s"]
    assert state.settings == {"LAST_AUTOACCEPTER_ACTIVATION": 1000, "last_accepted_game": 1001}
    assert not state.in_menu  # No extra local in-menu acceptance gate.


def test_none_coop_does_not_inspect_even_malformed_ticket_input():
    trace, _ = drive(iter_legacy_coop_accept(object()), lambda _: None)
    assert trace == [CoopAcceptRequest("read_coop")]


def test_empty_coop_token_represents_non_none_identity_and_is_captured_for_calls():
    state = SyntheticState()
    state.coop = CoopRef("")
    state.server = None
    trace, _ = drive(iter_legacy_coop_accept(()), state.respond)
    assert trace[-2:] == [
        CoopAcceptRequest("report_tickets", (CoopRef(""), (), False, None)),
        CoopAcceptRequest("consume_accept", (CoopRef(""),)),
    ]
    assert sum(step.operation == "read_coop" for step in trace) == 1


def test_coop_reference_is_not_reread_after_report_changes_observed_global():
    state = SyntheticState()
    captured = state.coop
    state.server = "other"

    def respond(step):
        if step.operation == "report_tickets":
            state.coop = CoopRef("replacement")
        return state.respond(step)

    trace, _ = drive(iter_legacy_coop_accept(ROWS), respond)
    calls = [
        step
        for step in trace
        if step.operation in ("report_tickets", "consume_accept", "leave_match")
    ]
    assert [step.arguments[0] for step in calls] == [captured] * 3


def test_empty_tickets_still_updates_set_observes_dependencies_and_reports_without_log():
    state = SyntheticState()
    state.server = None
    trace, _ = drive(iter_legacy_coop_accept(()), state.respond)
    assert [step.operation for step in trace] == [
        "read_coop",
        "write_coop_servers",
        "observe_all_in_menu",
        "observe_regions",
        "report_tickets",
        "consume_accept",
    ]
    assert state.servers == frozenset()


def test_normalization_preserves_order_duplicates_and_deduplicates_after_short_id():
    state = SyntheticState()
    state.server = None
    state.short_ids["other"] = "S"
    rows = (("two", "s", 100), ("one", "other", 101), ("two", "s", 102))
    trace, _ = drive(iter_legacy_coop_accept(rows), state.respond)
    report = next(step for step in trace if step.operation == "report_tickets")
    assert report.arguments[1] == (("id2", "s"), ("id1", "other"), ("id2", "s"))
    assert [step.arguments for step in trace if step.operation == "short_id"] == [
        ("s",),
        ("other",),
        ("s",),
    ]
    assert log_fields(trace, "TICKETS")["servers"] == ("S",)
    assert state.servers == frozenset(("s", "other"))


@pytest.mark.parametrize("server", [None, "", False, 0, 0.0, (), [], {}])
def test_falsey_ticket_server_skips_account_lookup_before_other_field_validation(server):
    state = SyntheticState()
    state.server = None
    trace, _ = drive(iter_legacy_coop_accept(((object(), server, object()),)), state.respond)
    assert not any(step.operation == "read_launched_account" for step in trace)
    assert log_fields(trace, "TICKETS")["found"] == 1
    assert log_fields(trace, "TICKETS")["sent"] == 0


@pytest.mark.parametrize("account", [None, False, 0, 0.0, "", (), [], {}, {"other": "ignored"}])
def test_falsey_account_or_missing_steamid_produces_no_ticket_and_does_not_read_timestamp(account):
    state = SyntheticState()
    state.accounts["one"] = account
    state.server = None
    trace, _ = drive(iter_legacy_coop_accept((("one", "s", object()),)), state.respond)
    assert log_fields(trace, "TICKETS")["sent"] == 0


@pytest.mark.parametrize(
    ("steamid", "expected"),
    [(None, "None"), (False, "False"), (0, "0"), (1.5, "1.5"), ("text", "text")],
)
def test_stored_steamid_uses_builtin_string_conversion_without_numeric_validation(
    steamid, expected
):
    state = SyntheticState()
    state.accounts["one"] = {"steamid": steamid}
    state.server = None
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert next(step.arguments[1] for step in trace if step.operation == "report_tickets") == (
        (expected, "s"),
    )


@pytest.mark.parametrize("logged_at", [None, False, 0, 0.0, "", (), [], {}, -20])
def test_falsey_or_negative_timestamp_has_zero_age_without_age_clock(logged_at):
    state = SyntheticState()
    trace, _ = drive(iter_legacy_coop_accept((("one", "s", logged_at),)), state.respond)
    assert CoopAcceptRequest("read_time", ("ticket_age",)) not in trace
    fields = log_fields(trace, "ACCEPTING")
    assert fields["age"] == 0.0
    assert type(fields["age"]) is float
    assert fields["wait"] == 4.0


def test_duplicate_timestamps_use_maximum_for_included_server():
    state = SyntheticState()
    state.clocks["ticket_age"] = 104
    trace, _ = drive(
        iter_legacy_coop_accept((("one", "s", 100), ("one", "s", 103), ("one", "s", 101))),
        state.respond,
    )
    assert log_fields(trace, "ACCEPTING")["age"] == 1
    assert log_fields(trace, "ACCEPTING")["wait"] == 3


@pytest.mark.parametrize("regions", [None, False, 0, "", ()])
def test_falsey_regions_substitution_affects_only_ticket_log(regions):
    state = SyntheticState()
    state.regions = regions
    state.server = None
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert log_fields(trace, "TICKETS")["regions"] == "any"
    assert (
        next(step.arguments[3] for step in trace if step.operation == "report_tickets") is regions
    )


@pytest.mark.parametrize(
    ("server", "enabled", "accepted", "last"),
    [
        (None, True, (), "consume_accept"),
        ("s", False, (), "read_setting"),
        ("s", True, ("s",), "accepted_games_contains"),
    ],
)
def test_report_consume_precede_short_circuit_gates(server, enabled, accepted, last):
    state = SyntheticState()
    state.server, state.autoaccept, state.accepted = server, enabled, list(accepted)
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    operations = [step.operation for step in trace]
    assert operations.index("report_tickets") < operations.index("consume_accept")
    assert operations[-1] == last
    assert "read_coop_servers" not in operations


def test_empty_consumed_server_is_not_none_and_reaches_refusal():
    state = SyntheticState()
    state.server = ""
    state.short_ids[""] = "empty"
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert CoopAcceptRequest("accepted_games_contains", ("",)) in trace
    assert trace[-1] == CoopAcceptRequest("leave_match", (state.coop, "match_cancelled"))


def test_refusal_uses_entire_late_set_refreshes_after_requested_short_id_and_logs_before_leave():
    state = SyntheticState()
    state.server_reads = iter((frozenset(("s", "other")), frozenset(("s", "other"))))
    state.short_ids["other"] = "S"
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    first_read = next(i for i, step in enumerate(trace) if step.operation == "read_coop_servers")
    assert trace[first_read : first_read + 3] == [
        CoopAcceptRequest("read_coop_servers"),
        CoopAcceptRequest("short_id", ("s",)),
        CoopAcceptRequest("read_coop_servers"),
    ]
    assert log_fields(trace, "ACCEPT_REFUSED")["current"] == ("S", "S")
    assert trace[-2].arguments == ("ACCEPT_REFUSED",)
    assert trace[-1] == CoopAcceptRequest("leave_match", (state.coop, "match_cancelled"))
    assert state.accepted == []


def test_refusal_log_failure_prevents_leave():
    state = SyntheticState()
    state.server_reads = iter((frozenset(), frozenset()))

    def respond(step):
        if step.operation == "coop_log" and step.arguments == ("ACCEPT_REFUSED",):
            state.observed.append(step)
            return RuntimeError("log_failed")
        return state.respond(step)

    with pytest.raises(RuntimeError, match="log_failed"):
        drive(iter_legacy_coop_accept(ROWS), respond)
    assert not any(step.operation == "leave_match" for step in state.observed)


def test_refusal_log_uses_refreshed_server_set_instead_of_the_failed_singleton_check():
    state = SyntheticState()
    state.server_reads = iter((frozenset(("s", "other")), frozenset(("other",))))
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert log_fields(trace, "ACCEPT_REFUSED")["current"] == ("O",)


@pytest.mark.parametrize(
    ("age", "delay"), [(-1, 4), (0, 4), (0.5, 3.5), (3.99, 0.01), (4, 0), (9, 0)]
)
def test_unrounded_age_thresholds_drive_delay(age, delay):
    state = SyntheticState()
    state.clocks["ticket_age"] = 100 + age
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    thread = next(step for step in trace if step.operation == "construct_thread")
    assert dict(thread.keywords)["args"][0] == pytest.approx(delay)


def test_two_global_reads_are_separate_and_in_expression_order():
    state = SyntheticState()
    state.minimums = iter((2.0, 9.0))
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert log_fields(trace, "ACCEPTING")["wait"] == 2
    assert sum(step.operation == "read_global" for step in trace) == 2


def test_changed_negative_global_retains_expression_without_artificial_delay_clamp():
    state = SyntheticState()
    state.minimums = iter((-2.0, 4.0))
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    thread = next(step for step in trace if step.operation == "construct_thread")
    assert dict(thread.keywords)["args"] == (-2.0, "s")


@pytest.mark.parametrize(
    ("log_enabled", "gate_enabled", "started"),
    [(True, False, False), (False, True, True), ("", ("enabled",), True)],
)
def test_enabled_is_reread_after_log_and_disabled_still_updates_state(
    log_enabled, gate_enabled, started
):
    state = SyntheticState()
    state.enabled = iter((log_enabled, gate_enabled))
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert log_fields(trace, "ACCEPTING")["enabled"] == log_enabled
    assert any(step.operation == "start_thread" for step in trace) is started
    assert state.accepted == ["s"]
    assert state.settings == {"LAST_AUTOACCEPTER_ACTIVATION": 1000, "last_accepted_game": 1001}


def test_log_rounds_age_wait_but_explicit_dispatcher_delay_retains_precision():
    state = SyntheticState()
    state.clocks["ticket_age"] = 101.26
    trace, _ = drive(iter_legacy_coop_accept(ROWS), state.respond)
    assert log_fields(trace, "ACCEPTING")["age"] == 1.3
    assert log_fields(trace, "ACCEPTING")["wait"] == 2.7
    thread = next(step for step in trace if step.operation == "construct_thread")
    assert dict(thread.keywords)["args"][0] == pytest.approx(2.74)


@pytest.mark.parametrize("failure_index", range(27))
def test_failure_at_every_success_path_request_stops_without_rollback_or_later_requests(
    failure_index,
):
    baseline = SyntheticState()
    expected, _ = drive(iter_legacy_coop_accept(ROWS), baseline.respond)
    state = SyntheticState()
    state.fail_index = failure_index
    sequence = iter_legacy_coop_accept(ROWS)
    with pytest.raises(RuntimeError, match="synthetic_dependency_failure"):
        drive(sequence, state.respond)
    assert state.observed == expected[: failure_index + 1]
    assert state.completed == [
        step for step in expected[:failure_index] if step in baseline.completed
    ]
    assert state.accepted == (["s"] if failure_index > 15 else [])
    assert ("LAST_AUTOACCEPTER_ACTIVATION" in state.settings) is (failure_index > 17)
    assert ("last_accepted_game" in state.settings) is (failure_index > 19)
    assert list(sequence) == []


def test_base_exception_propagates_after_append_without_rollback():
    state = SyntheticState()

    def respond(step):
        if step == CoopAcceptRequest("read_time", ("activation_timestamp",)):
            return KeyboardInterrupt()
        return state.respond(step)

    with pytest.raises(KeyboardInterrupt):
        drive(iter_legacy_coop_accept(ROWS), respond)
    assert state.accepted == ["s"]
    assert state.settings == {}


@pytest.mark.parametrize(
    "rows", [[("one", "s", 100)], (("one", "s"),), (("one", "s", 100, "extra"),)]
)
def test_malformed_ticket_shapes_fail_after_coop_observation(rows):
    state = SyntheticState()
    with pytest.raises(CoopAcceptPlanError):
        drive(iter_legacy_coop_accept(rows), state.respond)
    assert state.observed == [CoopAcceptRequest("read_coop")]


@pytest.mark.parametrize("value", ["100", True, float("inf"), float("nan")])
def test_truthy_invalid_timestamp_does_not_coerce_or_write_state(value):
    state = SyntheticState()
    with pytest.raises(CoopAcceptPlanError):
        drive(iter_legacy_coop_accept((("one", "s", value),)), state.respond)
    assert state.completed == []


def test_custom_hooks_are_never_called():
    class Explosive:
        def __bool__(self):
            raise AssertionError("truthiness hook")

        def __str__(self):
            raise AssertionError("string hook")

        def __float__(self):
            raise AssertionError("number hook")

    state = SyntheticState()
    state.accounts["one"] = {"steamid": Explosive()}
    with pytest.raises(CoopAcceptPlanError):
        drive(iter_legacy_coop_accept(ROWS), state.respond)
    with pytest.raises(CoopAcceptPlanError):
        drive(iter_legacy_coop_accept((("one", Explosive(), 0),)), SyntheticState().respond)


def test_records_are_frozen_and_close_executes_no_cleanup():
    sequence = iter_legacy_coop_accept(ROWS)
    step = next(sequence)
    with pytest.raises(FrozenInstanceError):
        setattr(step, "operation", "execute")
    sequence.close()
    assert list(sequence) == []
