"""Independent inert co-op state helper boundaries from checkpoint 04."""

import builtins
import socket
import threading
import time

import pytest

from fsm_panel.adapters.coop_state_plan import (
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


class MenuObservations:
    def __init__(self, *, length=1, entries=1, stamps=None, maps=None, activities=None):
        self.length = length
        self.keys = iter([GameKey("repeated-key")] * entries + [IterationEnd()])
        self.stamps = iter(stamps if stamps is not None else [65] * entries)
        self.maps = iter(maps if maps is not None else ["menu-map"] * entries)
        self.activities = iter(activities if activities is not None else ["menu"] * entries)
        self.entry_reads = 0
        self.clocks = 0
        self.last_entry = None

    def respond(self, request):
        op, args = request.operation, request.arguments
        if op == "game_info_length":
            return self.length
        if op == "game_info_iterator":
            return IteratorRef("live iterator")
        if op == "next_game_info_key":
            assert args == (IteratorRef("live iterator"),)
            return next(self.keys)
        if op == "read_time":
            self.clocks += 1
            return 100
        if op == "read_game_info_entry":
            self.entry_reads += 1
            self.last_entry = GameEntry("replacement-" + str(self.entry_reads))
            return self.last_entry
        assert op == "read_entry_path" and args[0] is self.last_entry
        return next(
            {
                ("provider", "timestamp"): self.stamps,
                ("map", "name"): self.maps,
                ("player", "activity"): self.activities,
            }[args[1]]
        )


def test_zero_length_bypasses_iteration_but_raising_length_propagates():
    trace, result = consume(iter_legacy_all_in_menu(), lambda _: 0)
    assert result is False and trace == [CoopStateRequest("game_info_length")]
    failure = RuntimeError("length unavailable")
    sequence = iter_legacy_all_in_menu()
    with pytest.raises(RuntimeError) as caught:
        consume(sequence, lambda _: failure)
    assert caught.value is failure and list(sequence) == []


@pytest.mark.parametrize("entries", [0, 3])
def test_live_iterator_is_not_limited_to_earlier_length_and_reloads_three_entries(entries):
    observations = MenuObservations(length=1, entries=entries)
    trace, result = consume(iter_legacy_all_in_menu(), observations.respond)
    assert result is True
    assert observations.clocks == entries and observations.entry_reads == entries * 3
    assert trace.count(CoopStateRequest("game_info_length")) == 1
    assert sum(item.operation == "next_game_info_key" for item in trace) == entries + 1
    for index, item in enumerate(trace):
        if item.operation == "read_time":
            assert trace[index + 1].operation == "read_game_info_entry"
            assert trace[index + 2].arguments[1] == ("provider", "timestamp")
            assert trace[index + 3].operation == "read_game_info_entry"
            assert trace[index + 4].arguments[1] == ("map", "name")
            assert trace[index + 5].operation == "read_game_info_entry"
            assert trace[index + 6].arguments[1] == ("player", "activity")
            assert (
                len(
                    {
                        trace[position].arguments[0].token
                        for position in (index + 2, index + 4, index + 6)
                    }
                )
                == 3
            )


@pytest.mark.parametrize("stamp,expected_reads", [(64.999, 1), (65, 3), (200, 3)])
def test_strict_age_boundary_skips_only_stale_entries(stamp, expected_reads):
    observations = MenuObservations(stamps=[stamp])
    trace, result = consume(iter_legacy_all_in_menu(), observations.respond)
    assert result is True and observations.entry_reads == expected_reads
    assert observations.clocks == 1
    assert sum(item.operation == "read_entry_path" for item in trace) == expected_reads


def test_later_clock_changes_freshness_instead_of_reusing_first_entry_time():
    observations = MenuObservations(entries=2, stamps=[65, 65], maps=["lobby"], activities=["menu"])
    clocks = iter((100, 101))

    def respond(request):
        answer = observations.respond(request)
        return next(clocks) if request.operation == "read_time" else answer

    trace, result = consume(iter_legacy_all_in_menu(), respond)
    assert result is True and observations.clocks == 2 and observations.entry_reads == 4
    assert (
        sum(
            item.operation == "read_entry_path" and item.arguments[1] == ("map", "name")
            for item in trace
        )
        == 1
    )


@pytest.mark.parametrize(
    "map_name,activity,paths", [("de_dust2", "menu", 2), ("", "Menu", 3), ("", "playing", 3)]
)
def test_rejecting_map_or_activity_ends_before_later_entries(map_name, activity, paths):
    observations = MenuObservations(entries=2, maps=[map_name], activities=[activity])
    trace, result = consume(iter_legacy_all_in_menu(), observations.respond)
    assert result is False and observations.clocks == 1
    assert sum(item.operation == "read_entry_path" for item in trace) == paths
    assert sum(item.operation == "next_game_info_key" for item in trace) == 1


def test_every_ordinary_entry_error_skips_that_entry_but_iterator_errors_return_false():
    baseline, _ = consume(iter_legacy_all_in_menu(), MenuObservations().respond)
    for index in range(1, len(baseline)):
        observations = MenuObservations()
        seen = []
        failure = RuntimeError("synthetic read failure")

        def respond(request):
            if len(seen) - 1 == index:
                return failure
            return observations.respond(request)

        trace, result = consume(iter_legacy_all_in_menu(), respond, seen)
        assert trace[: index + 1] == baseline[: index + 1]
        if baseline[index].operation in {"game_info_iterator", "next_game_info_key"}:
            assert result is False and trace == baseline[: index + 1]
        else:
            assert result is True
            assert trace[index + 1 :] == [
                CoopStateRequest("next_game_info_key", (IteratorRef("live iterator"),))
            ]


def test_nonempty_all_stale_or_failed_entries_returns_true_and_reads_each_clock():
    observations = MenuObservations(length=3, entries=3, stamps=[0])
    calls = 0

    def respond(request):
        nonlocal calls
        if request.operation == "read_time":
            calls += 1
            if calls > 1:
                return RuntimeError("clock unavailable for this entry")
        return observations.respond(request)

    trace, result = consume(iter_legacy_all_in_menu(), respond)
    assert result is True and calls == 3
    assert sum(item.operation == "next_game_info_key" for item in trace) == 4
    assert observations.entry_reads == 1


class Interrupted(BaseException):
    pass


def test_base_exceptions_at_every_menu_request_propagate_without_next_entry():
    baseline, _ = consume(iter_legacy_all_in_menu(), MenuObservations().respond)
    for index in range(len(baseline)):
        observations = MenuObservations()
        seen = []
        failure = Interrupted()
        sequence = iter_legacy_all_in_menu()
        with pytest.raises(Interrupted) as caught:
            consume(
                sequence,
                lambda request: (
                    failure if len(seen) - 1 == index else observations.respond(request)
                ),
                seen,
            )
        assert caught.value is failure and seen == baseline[: index + 1]
        assert list(sequence) == []


@pytest.mark.parametrize(
    "phase,expected",
    [
        ("game_info_iterator", False),
        ("next_game_info_key", True),
        ("read_time", True),
        ("read_entry_path", True),
    ],
)
def test_stop_iteration_is_exhaustion_only_at_next_and_an_entry_error_inside_body(phase, expected):
    observations = MenuObservations()
    seen = []
    injected = False

    def respond(request):
        nonlocal injected
        if request.operation == phase and not injected:
            injected = True
            return StopIteration("synthetic iterator completion")
        return observations.respond(request)

    trace, result = consume(iter_legacy_all_in_menu(), respond, seen)
    assert result is expected
    if phase in {"game_info_iterator", "next_game_info_key"}:
        assert trace[-1].operation == phase
    else:
        assert trace[-1].operation == "next_game_info_key"
        assert observations.clocks <= 1


class RegionObservations:
    def __init__(self, *, now=161, stamp=100, busy=False, missing=False):
        self.regions = RegionsRef("old regions")
        self.cache = RegionsCache(stamp, self.regions)
        self.now, self.busy, self.missing = now, busy, missing
        self.thread = ThreadRef("constructed")
        self.completed = []

    def respond(self, request):
        op, args = request.operation, request.arguments
        if op == "get_regions_cache":
            assert args[0] == "_srt_regions_cache"
            return CacheMissing() if self.missing else self.cache
        if op == "read_time":
            return self.now
        if op == "get_regions_busy":
            assert args == ("_srt_regions_busy", False)
            return self.busy
        if op == "write_regions_busy":
            assert args == (True,)
            self.busy = True
        elif op == "construct_thread":
            self.completed.append(request)
            return self.thread
        else:
            assert op == "start_thread" and args[0] is self.thread
            # A synchronous simulated worker may replace the global cache.
            self.cache = RegionsCache(999, RegionsRef("new regions after start"))
        self.completed.append(request)
        return None


@pytest.mark.parametrize(
    "now,busy,refresh,busy_reads",
    [
        (159, False, False, 0),
        (160, False, False, 0),
        (161, True, False, 1),
        (161, False, True, 1),
        (0, False, False, 0),
    ],
)
def test_regions_default_age_short_circuit_and_old_identity_even_after_refresh(
    now, busy, refresh, busy_reads
):
    observations = RegionObservations(now=now, busy=busy)
    old = observations.regions
    trace, result = consume(iter_legacy_regions(), observations.respond)
    assert result is old
    assert sum(item.operation == "get_regions_cache" for item in trace) == 1
    assert sum(item.operation == "get_regions_busy" for item in trace) == busy_reads
    assert sum(item.operation == "read_time" for item in trace) == 1
    assert [item.operation for item in observations.completed] == (
        ["write_regions_busy", "construct_thread", "start_thread"] if refresh else []
    )
    if refresh:
        assert observations.busy is True and observations.cache.regions is not old
        constructor = observations.completed[1]
        assert constructor.arguments == ()
        assert constructor.keywords == (("target", "self.bkcfWJfVrPJw"), ("daemon", True))


@pytest.mark.parametrize("missing", [False, True])
def test_every_cache_lookup_builds_fresh_default_and_returns_the_selected_identity(missing):
    defaults = []
    for _ in range(2):
        observations = RegionObservations(now=0, missing=missing)
        trace, result = consume(iter_legacy_regions(), observations.respond)
        default = trace[0].arguments[1]
        assert default.stamp == 0.0 and type(default.stamp) is float
        assert result is (default.regions if missing else observations.regions)
        assert observations.completed == []
        defaults.append(default)
    assert defaults[0] is not defaults[1] and defaults[0].regions is not defaults[1].regions


@pytest.mark.parametrize("invalid", [None, False, (), (0.0,)])
def test_present_malformed_cache_is_not_missing_and_fails_before_clock(invalid):
    seen = []
    sequence = iter_legacy_regions()
    with pytest.raises(CoopStatePlanError, match="^coop_state_plan_response_invalid$"):
        consume(sequence, lambda _: invalid, seen)
    assert len(seen) == 1 and seen[0].operation == "get_regions_cache"
    assert list(sequence) == []


@pytest.mark.parametrize(
    "phase",
    [
        "get_regions_cache",
        "get_regions_busy",
        "write_regions_busy",
        "construct_thread",
        "start_thread",
    ],
)
@pytest.mark.parametrize("error_type", [AttributeError, LookupError])
def test_attribute_error_defaults_apply_only_to_getattr_observations(phase, error_type):
    observations = RegionObservations()
    seen = []
    failure = error_type("synthetic attribute boundary")
    sequence = iter_legacy_regions()

    def respond(request):
        return failure if request.operation == phase else observations.respond(request)

    if error_type is AttributeError and phase in {"get_regions_cache", "get_regions_busy"}:
        trace, result = consume(sequence, respond, seen)
        expected = (
            trace[0].arguments[1].regions if phase == "get_regions_cache" else observations.regions
        )
        assert result is expected
        assert [item.operation for item in observations.completed] == [
            "write_regions_busy",
            "construct_thread",
            "start_thread",
        ]
    else:
        with pytest.raises(error_type) as caught:
            consume(sequence, respond, seen)
        assert caught.value is failure and seen[-1].operation == phase
        assert observations.busy is (phase in {"construct_thread", "start_thread"})
    assert list(sequence) == []


@pytest.mark.parametrize("maximum_age", [-60, 0, 10**100])
def test_supplied_maximum_age_retains_native_numeric_comparison(maximum_age):
    observations = RegionObservations(now=50)
    trace, result = consume(iter_legacy_regions(maximum_age), observations.respond)
    assert result is observations.regions
    assert any(item.operation == "construct_thread" for item in trace) is (-50 > maximum_age)


def test_regions_failure_at_every_request_preserves_busy_prefix_without_reset_or_return():
    baseline, _ = consume(iter_legacy_regions(), RegionObservations().respond)
    for index in range(len(baseline)):
        observations = RegionObservations()
        seen = []
        sequence = iter_legacy_regions()
        failure = RuntimeError("synthetic refresh failure")
        with pytest.raises(RuntimeError) as caught:
            consume(
                sequence,
                lambda request: (
                    failure if len(seen) - 1 == index else observations.respond(request)
                ),
                seen,
            )
        assert caught.value is failure and seen == baseline[: index + 1]
        write_position = next(
            position
            for position, item in enumerate(baseline)
            if item.operation == "write_regions_busy"
        )
        assert observations.busy is (index > write_position)
        assert observations.completed == [
            item
            for item in baseline[:index]
            if item.operation in {"write_regions_busy", "construct_thread", "start_thread"}
        ]
        assert list(sequence) == []


def test_invalid_menu_response_is_protocol_failure_not_a_swallowed_entry_error():
    observations = MenuObservations()
    sequence = iter_legacy_all_in_menu()
    with pytest.raises(CoopStatePlanError, match="^coop_state_plan_response_invalid$"):
        consume(
            sequence,
            lambda request: (
                "private invalid clock"
                if request.operation == "read_time"
                else observations.respond(request)
            ),
        )
    assert list(sequence) == []


def test_traces_forbid_real_dependencies_and_close_at_every_yield_without_cleanup(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("inert trace attempted a real dependency")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(time, "time", forbidden)
        patch.setattr(time, "sleep", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(threading.Thread, "__init__", forbidden)
        patch.setattr(threading.Thread, "start", forbidden)
        for factory, responder in (
            (iter_legacy_all_in_menu, MenuObservations),
            (iter_legacy_regions, RegionObservations),
        ):
            baseline, _ = consume(factory(), responder().respond)
            for stop in range(len(baseline)):
                sequence, observations = factory(), responder()
                request = next(sequence)
                for _ in range(stop):
                    request = sequence.send(observations.respond(request))
                sequence.close()
                assert list(sequence) == []
