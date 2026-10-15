"""Independent checkpoint-02 contracts, with supplied observations and no effects."""

import builtins
import random
import socket
import time

import pytest

from fsm_panel.adapters.autoaccept_plan import (
    AutoacceptPlanError,
    PixelWindow,
    TraceRequest,
    WindowRef,
    iter_legacy_abandonment,
    iter_legacy_background_batch,
    iter_legacy_dispatch,
    iter_legacy_foreground_pixels,
    iter_legacy_row_count,
    iter_legacy_title_activation,
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


def normal_dispatch_response(request):
    if request.operation == "read_time":
        return 120
    if request.operation in {"read_setting", "check_row_count", "check_abandonment"}:
        return False
    return None


@pytest.mark.parametrize("delay", [-5, -0.0, 0, 5e-324, 10**100])
def test_normal_dispatch_keeps_start_time_and_four_batch_order(delay):
    actual, result = consume(iter_legacy_dispatch(delay, "server"), normal_dispatch_response)
    expected = [
        TraceRequest("print_accept"),
        TraceRequest("read_time", ("dispatch_start",)),
        TraceRequest("read_setting", ("ALTERNATIVE_AUTOACCEPTER",), 1),
    ]
    if delay > 0:
        expected.append(TraceRequest("sleep", (delay,), 1))
    expected.extend(
        [
            TraceRequest("check_abandonment", ("server",), 1),
            TraceRequest("background_click_batch", (), 2),
            TraceRequest("sleep", (4,), 1),
            TraceRequest("check_abandonment", ("server",), 1),
            TraceRequest("background_click_batch", (), 2),
            TraceRequest("sleep", (4,), 1),
            TraceRequest("check_row_count", (120,), 1),
            TraceRequest("check_abandonment", ("server",), 1),
            TraceRequest("background_click_batch", (), 2),
            TraceRequest("sleep", (4,), 1),
            TraceRequest("check_row_count", (120,), 1),
            TraceRequest("check_abandonment", ("server",), 1),
            TraceRequest("background_click_batch", (), 2),
        ]
    )
    assert result is None and actual == expected


@pytest.mark.parametrize("stop_count", [1, 2])
def test_expanded_row_decision_short_circuits_abandonment_side_effects(stop_count):
    count_calls, abandoned = [], []

    def respond(request):
        if request.operation == "check_row_count":
            count_calls.append(request.arguments[0])
            rows = tuple(
                (None,) * 6 + (121,) for _ in range(6 if len(count_calls) == stop_count else 0)
            )
            _, decision = consume(
                iter_legacy_row_count(request.arguments[0]),
                lambda child: rows if child.operation == "read_account_rows" else False,
            )
            return decision
        if request.operation == "check_abandonment":
            # Before the count gate, the helper observes the exact singleton.
            trace, decision = consume(
                iter_legacy_abandonment("server"), lambda _: frozenset({"server"})
            )
            abandoned.extend(trace)
            return decision
        return normal_dispatch_response(request)

    trace, result = consume(iter_legacy_dispatch(1, "server"), respond)
    assert result is None and trace[-1].operation == "check_row_count"
    assert count_calls == [120] * stop_count
    assert abandoned == [TraceRequest("read_coop_servers")] * (stop_count + 1)
    assert sum(item.operation == "background_click_batch" for item in trace) == stop_count + 1


def test_alternative_never_requests_initial_delay_or_normal_helpers():
    trace, result = consume(
        iter_legacy_dispatch(10**100, "server"),
        lambda request: (
            True if request.operation == "read_setting" else normal_dispatch_response(request)
        ),
    )
    assert result is None
    assert trace == [
        TraceRequest("print_accept"),
        TraceRequest("read_time", ("dispatch_start",)),
        TraceRequest("read_setting", ("ALTERNATIVE_AUTOACCEPTER",), 1),
        TraceRequest("block_input", (True,), 2),
        TraceRequest("foreground_pixel_operation", (), 2),
        TraceRequest("block_input", (False,), 2),
    ]


def test_normal_dispatch_failure_at_any_request_propagates_without_recovery():
    baseline, _ = consume(iter_legacy_dispatch(1, "server"), normal_dispatch_response)
    for index in range(len(baseline)):
        seen = []
        failure = RuntimeError("synthetic dispatcher failure")
        sequence = iter_legacy_dispatch(1, "server")
        with pytest.raises(RuntimeError) as caught:
            consume(
                sequence,
                lambda request: (
                    failure if len(seen) - 1 == index else normal_dispatch_response(request)
                ),
                seen,
            )
        assert caught.value is failure
        assert seen == baseline[: index + 1] and list(sequence) == []


@pytest.mark.parametrize(
    "failed_request", ["foreground_pixel_operation", "print_traceback", "unblock"]
)
def test_alternative_catches_only_pixel_failure_and_unblocking_is_not_finally(failed_request):
    seen = []
    pixel_failure = RuntimeError("synthetic pixel failure")
    handler_failure = RuntimeError("synthetic handler failure")

    def respond(request):
        if request.operation == "read_setting":
            return True
        if request.operation == "foreground_pixel_operation":
            return pixel_failure
        if request.operation == failed_request or (
            failed_request == "unblock" and request == TraceRequest("block_input", (False,), 2)
        ):
            return handler_failure
        return normal_dispatch_response(request)

    sequence = iter_legacy_dispatch(1, "server")
    if failed_request == "foreground_pixel_operation":
        _, result = consume(sequence, respond, seen)
        assert result is None
        assert seen[-2:] == [
            TraceRequest("print_traceback", (), 2),
            TraceRequest("block_input", (False,), 2),
        ]
    else:
        with pytest.raises(RuntimeError) as caught:
            consume(sequence, respond, seen)
        assert caught.value is handler_failure
        assert seen[-1] == (
            TraceRequest("print_traceback", (), 2)
            if failed_request == "print_traceback"
            else TraceRequest("block_input", (False,), 2)
        )
    assert list(sequence) == []


class Untouched:
    def __repr__(self):
        raise AssertionError("opaque column represented")

    def __bool__(self):
        raise AssertionError("opaque column tested")

    def __gt__(self, other):
        raise AssertionError("opaque column compared")


@pytest.mark.parametrize("qualifying", [0, 1, 2, 5, 6, 7])
@pytest.mark.parametrize("mode", [False, True])
def test_row_count_reads_only_seventh_column_and_has_no_early_mode_read(qualifying, mode):
    opaque = Untouched()
    rows = tuple((opaque,) * 6 + (21, opaque) for _ in range(qualifying))
    rows += ((opaque,) * 6 + (20, opaque), (opaque,) * 6 + (-100, opaque))
    trace, result = consume(
        iter_legacy_row_count(20),
        lambda request: rows if request.operation == "read_account_rows" else mode,
    )
    assert result is (qualifying >= 6 or (mode and qualifying >= 2))
    assert trace == [TraceRequest("read_account_rows")] + (
        [] if qualifying >= 6 else [TraceRequest("read_setting", ("RUNNING2VS2",))]
    )


@pytest.mark.parametrize("last", [(), (0,) * 6 + ("private-invalid-timestamp",)])
def test_later_invalid_row_fails_after_six_good_rows_without_success_or_mode_read(last):
    sequence = iter_legacy_row_count(20)
    assert next(sequence) == TraceRequest("read_account_rows")
    with pytest.raises(AutoacceptPlanError, match="^autoaccept_plan_response_invalid$") as caught:
        sequence.send(((0,) * 6 + (21,),) * 6 + (last,))
    assert "private-invalid-timestamp" not in str(caught.value)
    assert list(sequence) == []


@pytest.mark.parametrize("server", [None, "server"])
def test_abandonment_bypasses_all_side_effects_for_none_or_exact_singleton(server):
    trace, result = consume(iter_legacy_abandonment(server), lambda _: frozenset({"server"}))
    assert result is False
    assert trace == ([] if server is None else [TraceRequest("read_coop_servers")])


def abandonment_response(*, present=True):
    reads = 0

    def respond(request):
        nonlocal reads
        if request.operation == "read_coop_servers":
            reads += 1
            return frozenset({"server", "extra"}) if reads == 1 else frozenset({"a", "b", "c"})
        if request.operation == "read_coop_present":
            return present
        if request.operation == "short_id":
            return {"server": "S", "a": "2", "b": "10", "c": "2"}[request.arguments[0]]
        return None

    return respond


@pytest.mark.parametrize("present", [False, True])
def test_abandonment_uses_fresh_set_after_leave_and_sorts_short_ids_without_deduplication(present):
    trace, result = consume(
        iter_legacy_abandonment("server"), abandonment_response(present=present)
    )
    assert result is True
    expected_prefix = [TraceRequest("read_coop_servers"), TraceRequest("read_coop_present")]
    if present:
        expected_prefix.append(TraceRequest("leave_match", ("match_cancelled",)))
    expected_prefix += [TraceRequest("short_id", ("server",)), TraceRequest("read_coop_servers")]
    assert trace[: len(expected_prefix)] == expected_prefix
    assert {item.arguments[0] for item in trace[len(expected_prefix) : -1]} == {"a", "b", "c"}
    assert trace[-1] == TraceRequest("log_accept_abandoned", ("S", ("10", "2", "2")))
    assert all(item.lock_depth == 0 for item in trace)


def test_abandonment_failure_at_any_dependency_prevents_true_return_and_further_calls():
    baseline, _ = consume(iter_legacy_abandonment("server"), abandonment_response())
    for index in range(len(baseline)):
        seen = []
        answer = abandonment_response()
        failure = RuntimeError("synthetic dependency failure")

        def respond(request):
            return failure if len(seen) - 1 == index else answer(request)

        sequence = iter_legacy_abandonment("server")
        with pytest.raises(RuntimeError) as caught:
            consume(sequence, respond, seen)
        assert caught.value is failure
        assert seen == baseline[: index + 1] and list(sequence) == []


DUPLICATE_WINDOW = WindowRef(0, "Counter-Strike 2 duplicate")
WINDOWS = (DUPLICATE_WINDOW, WindowRef(-2, "Counter-Strike 2 other"), DUPLICATE_WINDOW)


def batch_response():
    modes = iter((True, False, True))
    packed = iter(range(301, 310))

    def respond(request):
        if request.operation == "screen_size":
            return 0, 800
        if request.operation == "cursor_position":
            return 999, -8
        if request.operation == "enumerate_title_windows":
            return WINDOWS
        if request.operation == "shuffle_windows":
            assert request.arguments == (WINDOWS,)
            return WINDOWS[::-1]
        if request.operation == "read_setting":
            return next(modes)
        if request.operation == "make_long":
            return next(packed)
        return None

    return respond


def test_background_preserves_duplicate_windows_each_packed_value_and_none_button_up():
    trace, result = consume(iter_legacy_background_batch(), batch_response())
    assert result is None and all(item.lock_depth == 0 for item in trace)
    assert trace[:7] == [
        TraceRequest("block_input", (True,)),
        TraceRequest("screen_size"),
        TraceRequest("cursor_position"),
        TraceRequest("move_cursor", (-1, -8)),
        TraceRequest("enumerate_title_windows", ("Counter-Strike 2",)),
        TraceRequest("shuffle_windows", (WINDOWS,)),
        TraceRequest("sleep", (0.15,)),
    ]
    assert [item.arguments for item in trace if item.operation == "print_window"] == [
        (window.handle, window.title) for window in WINDOWS[::-1]
    ]
    messages = [item.arguments for item in trace if item.operation == "post_message"]
    assert messages == [
        (0, "WM_MOUSEMOVE", 0, 301),
        (0, "WM_LBUTTONDOWN", "VK_LBUTTON", 302),
        (0, "WM_LBUTTONUP", None, 303),
        (-2, "WM_MOUSEMOVE", 0, 304),
        (-2, "WM_LBUTTONDOWN", "VK_LBUTTON", 305),
        (-2, "WM_LBUTTONUP", None, 306),
        (0, "WM_MOUSEMOVE", 0, 307),
        (0, "WM_LBUTTONDOWN", "VK_LBUTTON", 308),
        (0, "WM_LBUTTONUP", None, 309),
    ]
    moves = [item.arguments for item in trace if item.operation == "make_long"]
    assert moves == [
        (201, 113),
        (0, 0),
        (0, 0),
        (175, 115),
        (0, 0),
        (0, 0),
        (201, 113),
        (0, 0),
        (0, 0),
    ]
    assert trace[-1] == TraceRequest("block_input", (False,))


def test_every_background_failure_keeps_the_whole_batch_exception_boundary():
    baseline, _ = consume(iter_legacy_background_batch(), batch_response())
    for index in range(len(baseline)):
        seen = []
        answer = batch_response()
        failure = RuntimeError("synthetic dependency failure")

        def respond(request):
            return failure if len(seen) - 1 == index else answer(request)

        sequence = iter_legacy_background_batch()
        if index in (0, len(baseline) - 1):
            with pytest.raises(RuntimeError) as caught:
                consume(sequence, respond, seen)
            assert caught.value is failure and seen == baseline[: index + 1]
        else:
            _, result = consume(sequence, respond, seen)
            assert result is None
            assert seen == baseline[: index + 1] + [TraceRequest("block_input", (False,))]
        assert list(sequence) == []


def pixel_response(*, sample=False):
    clocks = iter((100, 106.99, 99, 108) if sample else (100, 107))
    colors = iter(((255, 88, 255), (-1, 89, -1)))
    heights = iter((600, 610, 620) if sample else (600, 610, 620))
    modes = iter((True, False, True))
    activated = 0
    completed = 0

    def respond(request):
        nonlocal activated, completed
        if request.operation == "read_launched_accounts":
            return ("empty", "first", "late") if not sample else ("first",)
        if request.operation == "read_account_window_title":
            name = request.arguments[0]
            if name == "late":
                assert completed == 2
                return "late title after two clicks"
            return name
        if request.operation == "get_windows_with_title":
            if request.arguments == ("empty",):
                return ()
            return (PixelWindow("same"),) * (1 if sample or completed else 2)
        if request.operation == "read_window_title":
            return "live title"
        if request.operation == "activate_title":
            activated += 1
            return None
        if request.operation == "read_window_property":
            assert activated == completed + 1
            return {"left": 10, "top": 20, "width": -999, "height": 640}[request.arguments[1]]
        if request.operation == "read_time":
            return next(clocks)
        if request.operation == "read_setting":
            return next(modes) if request.arguments == ("RUNNING2VS2",) else next(heights)
        if request.operation == "pixel":
            return next(colors)
        if request.operation == "mouse_up":
            completed += 1
        return None

    return respond


def test_timeout_clicks_duplicates_and_late_accounts_without_polling_again():
    trace, result = consume(iter_legacy_foreground_pixels(), pixel_response())
    assert result is None
    assert [item.arguments for item in trace if item.operation == "read_time"] == [
        ("pixel_start",),
        ("pixel_elapsed",),
    ]
    assert not any(item.operation in {"pixel", "print_found_accept"} for item in trace)
    assert [item.arguments for item in trace if item.operation == "move_cursor"] == [
        (211, 173),
        (168, 163),
        (211, 153),
    ]
    assert sum(item.operation == "mouse_down" for item in trace) == 3
    assert sum(item.operation == "mouse_up" for item in trace) == 3
    assert all(item.lock_depth == 0 for item in trace)
    for index, item in enumerate(trace):
        if item.operation == "activate_title":
            assert trace[index + 1] == TraceRequest("sleep", (0.15,))
            assert [part.arguments[1] for part in trace[index + 2 : index + 6]] == [
                "left",
                "top",
                "width",
                "height",
            ]
        if item.operation == "mouse_down":
            assert item.arguments == ()
            assert trace[index - 1] == TraceRequest("sleep", (0.05,))
            assert trace[index + 1 : index + 3] == [
                TraceRequest("sleep", (0.14,)),
                TraceRequest("mouse_up"),
            ]


def test_poll_uses_only_green_strictly_above_88_and_fresh_heights_with_wall_time():
    trace, _ = consume(iter_legacy_foreground_pixels(), pixel_response(sample=True))
    assert [item.arguments for item in trace if item.operation == "pixel"] == [
        (168, 173),
        (168, 163),
    ]
    found = trace.index(TraceRequest("print_found_accept", (8,)))
    assert trace[found + 1] == TraceRequest("sleep", (2,))
    assert TraceRequest("move_cursor", (211, 153)) in trace
    assert sum(item == TraceRequest("sleep", (0.1,)) for item in trace) == 2
    for index, item in enumerate(trace):
        if item.operation == "pixel":
            assert trace[index - 2 : index] == [
                TraceRequest("sleep", (0.1,)),
                TraceRequest("read_setting", ("CS_HEIGHT",)),
            ]


@pytest.mark.parametrize(
    "failure_operation",
    [
        "activate_title",
        "read_window_property",
        "read_time",
        "read_setting",
        "mouse_down",
        "mouse_up",
    ],
)
def test_foreground_failure_aborts_all_remaining_windows_without_cleanup(failure_operation):
    seen = []
    answer = pixel_response()
    failure = RuntimeError("synthetic foreground failure")
    sequence = iter_legacy_foreground_pixels()
    with pytest.raises(RuntimeError) as caught:
        consume(
            sequence,
            lambda request: failure if request.operation == failure_operation else answer(request),
            seen,
        )
    assert caught.value is failure and seen[-1].operation == failure_operation
    assert not any(item.operation == "block_input" for item in seen)
    assert sum(item.operation == "activate_title" for item in seen) == 1
    assert list(sequence) == []


def test_activation_retries_all_three_stages_and_delays_after_last_failure():
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.operation == "find_window":
            attempts += 1
            return RuntimeError("find") if attempts == 1 else 0
        if request.operation == "set_foreground_window" and attempts == 2:
            return RuntimeError("foreground")
        if request.operation == "show_window":
            return RuntimeError("restore")
        return None

    trace, result = consume(iter_legacy_title_activation("title"), respond)
    assert result is None and trace == [
        TraceRequest("find_window", (None, "title"), 1),
        TraceRequest("sleep", (0.2,), 1),
        TraceRequest("find_window", (None, "title"), 1),
        TraceRequest("set_foreground_window", (0,), 1),
        TraceRequest("sleep", (0.2,), 1),
        TraceRequest("find_window", (None, "title"), 1),
        TraceRequest("set_foreground_window", (0,), 1),
        TraceRequest("show_window", (0, "SW_RESTORE"), 1),
        TraceRequest("sleep", (0.2,), 1),
    ]


def test_generators_never_execute_described_io_and_close_at_every_yield_adds_no_cleanup(
    monkeypatch,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("an inert trace attempted a real dependency")

    factories = [
        (lambda: iter_legacy_dispatch(1, "server"), lambda: normal_dispatch_response),
        (iter_legacy_background_batch, batch_response),
        (iter_legacy_foreground_pixels, pixel_response),
        (
            lambda: iter_legacy_title_activation("title"),
            lambda: lambda request: 0 if request.operation == "find_window" else None,
        ),
    ]
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(time, "time", forbidden)
        patch.setattr(time, "sleep", forbidden)
        patch.setattr(random, "shuffle", forbidden)
        patch.setattr(socket, "socket", forbidden)
        for factory, responder in factories:
            baseline, _ = consume(factory(), responder())
            for stop in range(len(baseline)):
                sequence = factory()
                answer = responder()
                request = next(sequence)
                for _ in range(stop):
                    request = sequence.send(answer(request))
                sequence.close()
                assert list(sequence) == []


@pytest.mark.parametrize("kind", ["background", "alternative"])
@pytest.mark.parametrize("error", [AutoacceptPlanError, GeneratorExit])
def test_protocol_and_base_exceptions_do_not_invent_ordinary_error_cleanup(kind, error):
    seen = []
    if kind == "background":
        sequence, answer, stop = iter_legacy_background_batch(), batch_response(), "post_message"
    else:
        sequence, stop = iter_legacy_dispatch(1, "server"), "foreground_pixel_operation"

        def answer(request):
            return (
                True if request.operation == "read_setting" else normal_dispatch_response(request)
            )

    with pytest.raises(error):
        consume(
            sequence,
            lambda request: error() if request.operation == stop else answer(request),
            seen,
        )
    assert seen[-1].operation == stop
    assert not any(item.operation in {"print_traceback", "mouse_up"} for item in seen)
    assert not any(item.operation == "block_input" and item.arguments == (False,) for item in seen)
    assert list(sequence) == []
