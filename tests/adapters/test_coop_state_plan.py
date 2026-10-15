from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from fsm_panel.adapters.coop_state_plan import (
    LEGACY_REGIONS_MAX_AGE,
    LEGACY_REGIONS_REFRESH_TARGET,
    CacheMissing,
    CoopStatePlanError,
    CoopStateRequest,
    GameEntry,
    GameKey,
    IterationEnd,
    IteratorRef,
    RegionsCache,
    RegionsRef,
    ThreadRef,
    iter_legacy_all_in_menu,
    iter_legacy_regions,
)


def drive(sequence, respond):
    trace: list[CoopStateRequest] = []
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


class MenuObservations:
    def __init__(self, *, keys=("one",), clocks=(35,)):
        self.length = len(keys)
        self.keys = iter(keys)
        self.clocks = iter(clocks)
        self.stamps = dict.fromkeys(keys, 0)
        self.maps = dict.fromkeys(keys, "menu")
        self.activities = dict.fromkeys(keys, "menu")
        self.lookup_count = 0
        self.observed = []
        self.failure_index = None
        self.failure: BaseException = RuntimeError("dependency_failed")

    def respond(self, step):
        self.observed.append(step)
        if len(self.observed) - 1 == self.failure_index:
            return self.failure
        op, args = step.operation, step.arguments
        if op == "game_info_length":
            return self.length
        if op == "game_info_iterator":
            return IteratorRef("iteration")
        if op == "next_game_info_key":
            key = next(self.keys, None)
            return IterationEnd() if key is None else GameKey(key)
        if op == "read_time":
            return next(self.clocks)
        if op == "read_game_info_entry":
            self.lookup_count += 1
            return GameEntry(f"{args[0].token}/{self.lookup_count}")
        if op == "read_entry_path":
            key = args[0].token.split("/")[0]
            if args[1] == ("provider", "timestamp"):
                return self.stamps[key]
            if args[1] == ("map", "name"):
                return self.maps[key]
            assert args[1] == ("player", "activity")
            return self.activities[key]
        raise AssertionError(f"Unexpected request: {op}")


def test_menu_trace_reads_clock_then_new_entry_for_every_path():
    state = MenuObservations()
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is True
    assert trace == [
        CoopStateRequest("game_info_length"),
        CoopStateRequest("game_info_iterator"),
        CoopStateRequest("next_game_info_key", (IteratorRef("iteration"),)),
        CoopStateRequest("read_time", ("entry_age",)),
        CoopStateRequest("read_game_info_entry", (GameKey("one"),)),
        CoopStateRequest("read_entry_path", (GameEntry("one/1"), ("provider", "timestamp"))),
        CoopStateRequest("read_game_info_entry", (GameKey("one"),)),
        CoopStateRequest("read_entry_path", (GameEntry("one/2"), ("map", "name"))),
        CoopStateRequest("read_game_info_entry", (GameKey("one"),)),
        CoopStateRequest("read_entry_path", (GameEntry("one/3"), ("player", "activity"))),
        CoopStateRequest("next_game_info_key", (IteratorRef("iteration"),)),
    ]


def test_empty_game_info_returns_false_without_iterator_or_clock():
    state = MenuObservations(keys=(), clocks=())
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert trace == [CoopStateRequest("game_info_length")]
    assert result is False


def test_length_failure_is_outside_outer_handler():
    state = MenuObservations()
    state.failure_index = 0
    with pytest.raises(RuntimeError, match="dependency_failed"):
        drive(iter_legacy_all_in_menu(), state.respond)
    assert len(state.observed) == 1


@pytest.mark.parametrize("index", [1, 2, 10])
def test_iterator_creation_and_advancement_errors_return_false(index):
    state = MenuObservations()
    state.failure_index = index
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is False
    assert len(trace) == index + 1


@pytest.mark.parametrize(("index", "expected"), [(1, False), (2, True), (3, True), (10, True)])
def test_stop_iteration_uses_its_exact_iterator_or_entry_boundary(index, expected):
    state = MenuObservations()
    state.failure_index, state.failure = index, StopIteration("normal-or-dependent")
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is expected
    if index == 3:
        assert trace[-1].operation == "next_game_info_key"
        assert len(trace) == index + 2
    else:
        assert len(trace) == index + 1


@pytest.mark.parametrize("index", range(3, 10))
def test_per_entry_errors_skip_remaining_entry_then_nonempty_exhaustion_returns_true(index):
    state = MenuObservations()
    state.failure_index = index
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is True
    assert trace[-1].operation == "next_game_info_key"
    assert len(trace) == index + 2


@pytest.mark.parametrize("index", range(11))
def test_base_exception_propagates_at_every_menu_request(index):
    state = MenuObservations()
    state.failure_index, state.failure = index, KeyboardInterrupt()
    sequence = iter_legacy_all_in_menu()
    with pytest.raises(KeyboardInterrupt):
        drive(sequence, state.respond)
    assert len(state.observed) == index + 1
    assert list(sequence) == []


@pytest.mark.parametrize(
    ("age", "expected", "checks_map"),
    [(35.01, True, False), (35, False, True), (34, False, True), (-10, False, True)],
)
def test_stale_boundary_is_strict_and_fresh_underscore_short_circuits_activity(
    age, expected, checks_map
):
    state = MenuObservations(clocks=(age,))
    state.maps["one"] = "de_anything"
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is expected
    assert (
        any(
            step.operation == "read_entry_path" and step.arguments[1] == ("map", "name")
            for step in trace
        )
        is checks_map
    )
    assert not any(
        step.operation == "read_entry_path" and step.arguments[1] == ("player", "activity")
        for step in trace
    )


@pytest.mark.parametrize("activity", ["playing", "MENU", "", " menu"])
def test_activity_requires_exact_menu(activity):
    state = MenuObservations()
    state.activities["one"] = activity
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is False
    assert trace[-1].arguments[1] == ("player", "activity")


def test_fresh_clock_per_key_can_make_second_entry_fresh_after_stale_first():
    state = MenuObservations(keys=("one", "two"), clocks=(36, 35))
    state.maps["two"] = "de_second"
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is False
    assert sum(step.operation == "read_time" for step in trace) == 2
    assert state.lookup_count == 3


def test_all_stale_nonempty_returns_true_without_map_or_activity_reads():
    state = MenuObservations(keys=("one", "two"), clocks=(36, 100))
    trace, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is True
    assert all(
        step.arguments[1] == ("provider", "timestamp")
        for step in trace
        if step.operation == "read_entry_path"
    )


def test_all_malformed_nonempty_entries_are_skipped():
    state = MenuObservations(keys=("one", "two"), clocks=(1, 2))

    def respond(step):
        if step.operation == "read_game_info_entry":
            state.observed.append(step)
            return KeyError("missing_entry")
        return state.respond(step)

    _, result = drive(iter_legacy_all_in_menu(), respond)
    assert result is True
    assert sum(step.operation == "read_game_info_entry" for step in state.observed) == 2


def test_nonzero_length_followed_by_immediate_exhaustion_returns_true():
    state = MenuObservations(keys=(), clocks=())
    state.length = 1
    _, result = drive(iter_legacy_all_in_menu(), state.respond)
    assert result is True


@pytest.mark.parametrize("index", range(11))
def test_invalid_menu_responses_propagate_even_inside_both_legacy_handlers(index):
    state = MenuObservations()

    def respond(step):
        if len(state.observed) == index:
            state.observed.append(step)
            return None
        return state.respond(step)

    sequence = iter_legacy_all_in_menu()
    with pytest.raises(CoopStatePlanError, match="^coop_state_plan_response_invalid$"):
        drive(sequence, respond)
    assert len(state.observed) == index + 1
    assert list(sequence) == []


class RegionsObservations:
    def __init__(self):
        self.regions = RegionsRef("cached-object")
        self.cache = RegionsCache(100, self.regions)
        self.now = 161
        self.busy = False
        self.observed = []
        self.completed = []
        self.failure_index = None

    def respond(self, step):
        self.observed.append(step)
        if len(self.observed) - 1 == self.failure_index:
            return RuntimeError("regions_dependency_failed")
        op = step.operation
        if op == "get_regions_cache":
            return self.cache
        if op == "read_time":
            return self.now
        if op == "get_regions_busy":
            return self.busy
        if op == "write_regions_busy":
            self.busy = step.arguments[0]
        elif op == "construct_thread":
            self.completed.append(step)
            return ThreadRef("refresh-thread")
        elif op != "start_thread":
            raise AssertionError(f"Unexpected request: {op}")
        self.completed.append(step)
        return None


def test_regions_default_age_and_refresh_sequence_return_original_object():
    state = RegionsObservations()
    trace, result = drive(iter_legacy_regions(), state.respond)
    assert LEGACY_REGIONS_MAX_AGE == 60.0
    assert result is state.regions
    assert trace[0].operation == "get_regions_cache"
    assert trace[0].arguments[0] == "_srt_regions_cache"
    default = trace[0].arguments[1]
    assert isinstance(default, RegionsCache)
    assert default.stamp == 0.0
    assert trace[1:] == [
        CoopStateRequest("read_time", ("regions_age",)),
        CoopStateRequest("get_regions_busy", ("_srt_regions_busy", False)),
        CoopStateRequest("write_regions_busy", (True,)),
        CoopStateRequest(
            "construct_thread",
            keywords=(("target", LEGACY_REGIONS_REFRESH_TARGET), ("daemon", True)),
        ),
        CoopStateRequest("start_thread", (ThreadRef("refresh-thread"),)),
    ]
    assert state.busy is True


@pytest.mark.parametrize("age", [-10, 0, 59.99, 60])
def test_regions_busy_is_not_read_until_strictly_after_max_age(age):
    state = RegionsObservations()
    state.now = 100 + age
    trace, result = drive(iter_legacy_regions(), state.respond)
    assert result is state.regions
    assert [step.operation for step in trace] == ["get_regions_cache", "read_time"]


def test_expired_busy_cache_returns_old_object_without_state_or_thread_changes():
    state = RegionsObservations()
    state.busy = True
    trace, result = drive(iter_legacy_regions(), state.respond)
    assert result is state.regions
    assert trace[-1].operation == "get_regions_busy"
    assert state.completed == []


def test_explicit_negative_max_age_is_not_clamped():
    state = RegionsObservations()
    state.now = 100
    trace, _ = drive(iter_legacy_regions(-1), state.respond)
    assert trace[-1].operation == "start_thread"


def test_thread_start_cache_replacement_is_not_reread_before_return():
    state = RegionsObservations()
    original = state.regions

    def respond(step):
        if step.operation == "start_thread":
            state.cache = RegionsCache(161, RegionsRef("replacement"))
        return state.respond(step)

    trace, result = drive(iter_legacy_regions(), respond)
    assert result is original
    assert result is not state.cache.regions
    assert sum(step.operation == "get_regions_cache" for step in trace) == 1


def test_missing_cache_uses_fresh_default_empty_regions_identity():
    results = []
    for _ in range(2):
        defaults = []

        def respond(step):
            if step.operation == "get_regions_cache":
                defaults.append(step.arguments[1])
                return CacheMissing()
            assert step.operation == "read_time"
            return 60

        _, result = drive(iter_legacy_regions(), respond)
        assert result is defaults[0].regions
        results.append(result)
    assert results[0] is not results[1]


def test_getattr_attribute_errors_use_cache_and_busy_defaults_without_writing_cache():
    state = RegionsObservations()
    defaults = []

    def respond(step):
        if step.operation == "get_regions_cache":
            defaults.append(step.arguments[1])
            return AttributeError("cache_absent")
        if step.operation == "get_regions_busy":
            return AttributeError("busy_absent")
        return state.respond(step)

    trace, result = drive(iter_legacy_regions(), respond)
    assert result is defaults[0].regions
    assert state.busy is True
    assert trace[-1].operation == "start_thread"
    assert not any(step.operation == "write_regions_cache" for step in trace)


@pytest.mark.parametrize("cache", [None, False, (), (0,), (0, None, "extra")])
def test_present_invalid_cache_does_not_use_default_or_read_clock(cache):
    sequence = iter_legacy_regions()
    assert next(sequence).operation == "get_regions_cache"
    with pytest.raises(CoopStatePlanError):
        sequence.send(cache)
    assert list(sequence) == []


@pytest.mark.parametrize("index", range(6))
def test_regions_failures_propagate_and_leave_acknowledged_busy_write_in_place(index):
    state = RegionsObservations()
    state.failure_index = index
    sequence = iter_legacy_regions()
    with pytest.raises(RuntimeError, match="regions_dependency_failed"):
        drive(sequence, state.respond)
    assert len(state.observed) == index + 1
    assert state.busy is (index > 3)
    assert not any(
        step == CoopStateRequest("write_regions_busy", (False,)) for step in state.observed
    )
    assert list(sequence) == []


def test_regions_base_exception_after_busy_write_does_not_clear_busy():
    state = RegionsObservations()

    def respond(step):
        if step.operation == "construct_thread":
            return KeyboardInterrupt()
        return state.respond(step)

    with pytest.raises(KeyboardInterrupt):
        drive(iter_legacy_regions(), respond)
    assert state.busy is True


@pytest.mark.parametrize("value", [True, None, "60", float("nan"), float("inf")])
def test_invalid_max_age_fails_before_any_observation(value):
    with pytest.raises(CoopStatePlanError):
        next(iter_legacy_regions(value))


@pytest.mark.parametrize("index", range(6))
def test_invalid_regions_response_is_terminal_without_reset(index):
    state = RegionsObservations()

    def respond(step):
        if len(state.observed) == index:
            state.observed.append(step)
            return "invalid"
        return state.respond(step)

    with pytest.raises(CoopStatePlanError):
        drive(iter_legacy_regions(), respond)
    assert len(state.observed) == index + 1
    assert state.busy is (index > 3)


def test_custom_values_never_run_numeric_or_truthiness_hooks():
    class Explosive:
        def __float__(self):
            raise AssertionError("numeric hook")

        def __bool__(self):
            raise AssertionError("truthiness hook")

    with pytest.raises(CoopStatePlanError):
        next(iter_legacy_regions(cast(float, Explosive())))
    state = RegionsObservations()

    def respond(step):
        return Explosive() if step.operation == "get_regions_busy" else state.respond(step)

    with pytest.raises(CoopStatePlanError):
        drive(iter_legacy_regions(), respond)


def test_frozen_requests_and_close_perform_no_cleanup():
    sequence = iter_legacy_regions()
    request = next(sequence)
    with pytest.raises(FrozenInstanceError):
        setattr(request, "operation", "execute")
    sequence.close()
    assert list(sequence) == []
