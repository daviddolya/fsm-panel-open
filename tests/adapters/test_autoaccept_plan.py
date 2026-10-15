from dataclasses import FrozenInstanceError

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


def drive(sequence, respond):
    trace: list[TraceRequest] = []
    try:
        step = next(sequence)
    except StopIteration as done:
        return trace, done.value
    while True:
        trace.append(step)
        response = respond(step)
        try:
            if isinstance(response, BaseException):
                step = sequence.throw(response)
            else:
                step = sequence.send(response)
        except StopIteration as done:
            return trace, done.value


def dispatcher_response(step):
    if step.operation == "read_time":
        return 100
    if step.operation in ("read_setting", "check_row_count", "check_abandonment"):
        return False
    return None


@pytest.mark.parametrize("delay", [0, -1, -1.5])
def test_dispatch_checks_count_only_before_third_and_fourth_batches(delay):
    trace, result = drive(iter_legacy_dispatch(delay, "server"), dispatcher_response)
    assert result is None
    assert trace == [
        TraceRequest("print_accept"),
        TraceRequest("read_time", ("dispatch_start",)),
        TraceRequest("read_setting", ("ALTERNATIVE_AUTOACCEPTER",), 1),
        TraceRequest("check_abandonment", ("server",), 1),
        TraceRequest("background_click_batch", (), 2),
        TraceRequest("sleep", (4,), 1),
        TraceRequest("check_abandonment", ("server",), 1),
        TraceRequest("background_click_batch", (), 2),
        TraceRequest("sleep", (4,), 1),
        TraceRequest("check_row_count", (100,), 1),
        TraceRequest("check_abandonment", ("server",), 1),
        TraceRequest("background_click_batch", (), 2),
        TraceRequest("sleep", (4,), 1),
        TraceRequest("check_row_count", (100,), 1),
        TraceRequest("check_abandonment", ("server",), 1),
        TraceRequest("background_click_batch", (), 2),
    ]


def test_positive_delay_is_after_start_and_setting_and_before_first_abandonment():
    trace, _ = drive(iter_legacy_dispatch(10**100, None), dispatcher_response)
    assert trace[1:5] == [
        TraceRequest("read_time", ("dispatch_start",)),
        TraceRequest("read_setting", ("ALTERNATIVE_AUTOACCEPTER",), 1),
        TraceRequest("sleep", (10**100,), 1),
        TraceRequest("check_abandonment", (None,), 1),
    ]


def test_late_count_true_short_circuits_abandonment_and_remaining_batches():
    batches = 0

    def respond(step):
        nonlocal batches
        if step.operation == "background_click_batch":
            batches += 1
        if step.operation == "check_row_count":
            assert batches == 2
            return True
        return dispatcher_response(step)

    trace, _ = drive(iter_legacy_dispatch(0, "server"), respond)
    assert trace[-1] == TraceRequest("check_row_count", (100,), 1)
    assert batches == 2
    assert sum(step.operation == "check_abandonment" for step in trace) == 2


@pytest.mark.parametrize("abandon_call", [1, 2, 3, 4])
def test_abandonment_stops_at_each_late_check(abandon_call):
    checks = 0

    def respond(step):
        nonlocal checks
        if step.operation == "check_abandonment":
            checks += 1
            return checks == abandon_call
        return dispatcher_response(step)

    trace, _ = drive(iter_legacy_dispatch(0, "server"), respond)
    assert trace[-1].operation == "check_abandonment"
    assert sum(step.operation == "background_click_batch" for step in trace) == abandon_call - 1


def test_alternative_ignores_delay_and_all_normal_predicates():
    def respond(step):
        if step.operation == "read_setting":
            return True
        return dispatcher_response(step)

    trace, result = drive(iter_legacy_dispatch(500, "server"), respond)
    assert result is None
    assert trace[3:] == [
        TraceRequest("block_input", (True,), 2),
        TraceRequest("foreground_pixel_operation", (), 2),
        TraceRequest("block_input", (False,), 2),
    ]


@pytest.mark.parametrize("failure", ["block_input", "print_traceback"])
def test_alternative_failures_outside_pixel_handler_propagate(failure):
    def respond(step):
        if step.operation == "read_setting":
            return True
        if step.operation == "foreground_pixel_operation":
            return RuntimeError("pixel_failed")
        if step.operation == failure:
            return RuntimeError("outside_pixel_handler")
        return dispatcher_response(step)

    with pytest.raises(RuntimeError, match="outside_pixel_handler"):
        drive(iter_legacy_dispatch(0, None), respond)


def test_alternative_ordinary_pixel_error_prints_then_unblocks():
    def respond(step):
        if step.operation == "read_setting":
            return True
        if step.operation == "foreground_pixel_operation":
            return RuntimeError("pixel_failed")
        return dispatcher_response(step)

    trace, _ = drive(iter_legacy_dispatch(0, None), respond)
    assert trace[-2:] == [
        TraceRequest("print_traceback", (), 2),
        TraceRequest("block_input", (False,), 2),
    ]


def test_base_exception_does_not_invent_unblocking_or_mouse_release():
    observed = []

    def respond(step):
        observed.append(step)
        if step.operation == "read_setting":
            return True
        if step.operation == "foreground_pixel_operation":
            return KeyboardInterrupt()
        return dispatcher_response(step)

    sequence = iter_legacy_dispatch(0, None)
    with pytest.raises(KeyboardInterrupt):
        drive(sequence, respond)
    assert observed[-1].operation == "foreground_pixel_operation"
    assert list(sequence) == []


def rows(*timestamps):
    return tuple((None,) * 6 + (timestamp,) for timestamp in timestamps)


@pytest.mark.parametrize(
    ("timestamps", "mode", "expected", "read_mode"),
    [
        ((10, 11), True, False, True),
        ((11, 12), True, True, True),
        ((11, 12), False, False, True),
        ((11,) * 5, False, False, True),
        ((11,) * 6, False, True, False),
        ((), True, False, True),
    ],
)
def test_row_count_strict_time_inclusive_threshold_and_deferred_mode(
    timestamps, mode, expected, read_mode
):
    def respond(step):
        if step.operation == "read_account_rows":
            return rows(*timestamps)
        assert read_mode
        assert step == TraceRequest("read_setting", ("RUNNING2VS2",))
        return mode

    trace, result = drive(iter_legacy_row_count(10), respond)
    assert result is expected
    assert len(trace) == 1 + read_mode


def test_row_count_does_not_stop_at_six_before_a_later_malformed_row():
    sequence = iter_legacy_row_count(10)
    assert next(sequence) == TraceRequest("read_account_rows")
    with pytest.raises(AutoacceptPlanError):
        sequence.send(rows(*(11,) * 6) + ((),))


@pytest.mark.parametrize("server", [None, "server"])
def test_none_or_exact_singleton_returns_false_without_coop_reads(server):
    trace, result = drive(iter_legacy_abandonment(server), lambda _: frozenset(("server",)))
    assert result is False
    assert trace == ([] if server is None else [TraceRequest("read_coop_servers")])


@pytest.mark.parametrize("coop_present", [False, True])
def test_abandonment_uses_whole_set_then_late_current_set_for_sorted_short_ids(coop_present):
    reads = 0

    def respond(step):
        nonlocal reads
        if step.operation == "read_coop_servers":
            reads += 1
            return frozenset(("server", "extra")) if reads == 1 else frozenset(("z", "a"))
        if step.operation == "read_coop_present":
            return coop_present
        if step.operation == "short_id":
            return {"server": "S", "z": "A", "a": "Z"}[step.arguments[0]]
        return None

    trace, result = drive(iter_legacy_abandonment("server"), respond)
    assert result is True
    assert reads == 2
    assert trace[-1] == TraceRequest("log_accept_abandoned", ("S", ("A", "Z")))
    assert (TraceRequest("leave_match", ("match_cancelled",)) in trace) is coop_present
    assert trace.index(TraceRequest("short_id", ("server",))) < len(trace) - 3


@pytest.mark.parametrize("failure", ["leave_match", "log_accept_abandoned"])
def test_abandonment_effect_errors_prevent_true_return(failure):
    def respond(step):
        if step.operation == "read_coop_servers":
            return frozenset()
        if step.operation == "read_coop_present":
            return True
        if step.operation == "short_id":
            return "short"
        if step.operation == failure:
            return RuntimeError("co_op_dependency_failed")
        return None

    with pytest.raises(RuntimeError, match="co_op_dependency_failed"):
        drive(iter_legacy_abandonment("server"), respond)


WINDOWS = (WindowRef(0, "Counter-Strike 2 one"), WindowRef(2, "Counter-Strike 2 two"))


def batch_response(step):
    if step.operation == "screen_size":
        return 1000, 800
    if step.operation == "cursor_position":
        return 99, 50
    if step.operation == "enumerate_title_windows":
        return WINDOWS
    if step.operation == "shuffle_windows":
        return WINDOWS[::-1]
    if step.operation == "read_setting":
        return False
    if step.operation == "make_long":
        return 123
    return None


def test_background_shuffle_order_late_mode_and_exact_message_parameters():
    modes = iter((False, True))

    def respond(step):
        return next(modes) if step.operation == "read_setting" else batch_response(step)

    trace, _ = drive(iter_legacy_background_batch(), respond)
    assert trace[:7] == [
        TraceRequest("block_input", (True,)),
        TraceRequest("screen_size"),
        TraceRequest("cursor_position"),
        TraceRequest("move_cursor", (999, 50)),
        TraceRequest("enumerate_title_windows", ("Counter-Strike 2",)),
        TraceRequest("shuffle_windows", (WINDOWS,)),
        TraceRequest("sleep", (0.15,)),
    ]
    assert trace[7:17] == [
        TraceRequest("print_window", (2, "Counter-Strike 2 two")),
        TraceRequest("read_setting", ("RUNNING2VS2",)),
        TraceRequest("make_long", (175, 115)),
        TraceRequest("post_message", (2, "WM_MOUSEMOVE", 0, 123)),
        TraceRequest("make_long", (0, 0)),
        TraceRequest("post_message", (2, "WM_LBUTTONDOWN", "VK_LBUTTON", 123)),
        TraceRequest("make_long", (0, 0)),
        TraceRequest("post_message", (2, "WM_LBUTTONUP", None, 123)),
        TraceRequest("sleep", (0.1,)),
        TraceRequest("print_window", (0, "Counter-Strike 2 one")),
    ]
    assert TraceRequest("make_long", (201, 113)) in trace
    assert trace[-1] == TraceRequest("block_input", (False,))


@pytest.mark.parametrize("failure", ["screen_size", "post_message", "sleep"])
def test_background_exception_skips_entire_remaining_batch_then_unblocks(failure):
    def respond(step):
        return RuntimeError("batch_failed") if step.operation == failure else batch_response(step)

    trace, _ = drive(iter_legacy_background_batch(), respond)
    assert trace[-2].operation == failure
    assert trace[-1] == TraceRequest("block_input", (False,))
    assert not any(step.operation == "post_message" for step in trace[trace.index(trace[-2]) + 1 :])


@pytest.mark.parametrize("enabled", [True, False])
def test_background_block_calls_are_outside_handler(enabled):
    def respond(step):
        if step == TraceRequest("block_input", (enabled,)):
            return RuntimeError("block_failed")
        return batch_response(step)

    with pytest.raises(RuntimeError, match="block_failed"):
        drive(iter_legacy_background_batch(), respond)


def pixel_driver(*, times, pixels=(), modes=(False,), heights=(600,), account_windows=None):
    times, pixels, modes, heights = map(iter, (times, pixels, modes, heights))
    windows_by_title = account_windows or {"first": (PixelWindow("w1"),)}

    def respond(step):
        if step.operation == "read_launched_accounts":
            return tuple(windows_by_title)
        if step.operation == "read_account_window_title":
            return step.arguments[0]
        if step.operation == "get_windows_with_title":
            return windows_by_title[step.arguments[0]]
        if step.operation == "read_window_title":
            return "window title"
        if step.operation == "read_window_property":
            return {"left": 10, "top": 20, "width": 800, "height": 640}[step.arguments[1]]
        if step.operation == "read_time":
            return next(times)
        if step.operation == "read_setting":
            return next(modes) if step.arguments == ("RUNNING2VS2",) else next(heights)
        if step.operation == "pixel":
            return next(pixels)
        return None

    return respond


def test_pixel_timeout_clicks_first_and_subsequent_windows_without_second_poll():
    respond = pixel_driver(
        times=(100, 107),
        modes=(True, False),
        heights=(600, 610),
        account_windows={"empty": (), "first": (PixelWindow("w1"), PixelWindow("w2"))},
    )
    trace, _ = drive(iter_legacy_foreground_pixels(), respond)
    assert [step for step in trace if step.operation == "read_time"] == [
        TraceRequest("read_time", ("pixel_start",)),
        TraceRequest("read_time", ("pixel_elapsed",)),
    ]
    assert not any(step.operation == "pixel" for step in trace)
    assert [step for step in trace if step.operation == "move_cursor"] == [
        TraceRequest("move_cursor", (211, 173)),
        TraceRequest("move_cursor", (168, 163)),
    ]
    assert sum(step.operation == "mouse_down" for step in trace) == 2
    assert trace[-4:] == [
        TraceRequest("sleep", (0.05,)),
        TraceRequest("mouse_down"),
        TraceRequest("sleep", (0.14,)),
        TraceRequest("mouse_up"),
    ]


def test_pixel_green_threshold_and_late_height_then_success_delay_and_two_v_two_click():
    respond = pixel_driver(
        times=(100, 100, 106.99, 107.2),
        pixels=((255, 88, 255), (0, 89, 0)),
        modes=(True,),
        heights=(600, 610, 620),
    )
    trace, _ = drive(iter_legacy_foreground_pixels(), respond)
    assert [step for step in trace if step.operation == "pixel"] == [
        TraceRequest("pixel", (168, 173)),
        TraceRequest("pixel", (168, 163)),
    ]
    found = next(i for i, step in enumerate(trace) if step.operation == "print_found_accept")
    assert trace[found].arguments[0] == pytest.approx(7.2)
    assert trace[found + 1] == TraceRequest("sleep", (2,))
    assert TraceRequest("move_cursor", (211, 153)) in trace
    assert sum(step == TraceRequest("sleep", (0.1,)) for step in trace) == 2


def test_pixel_wall_clock_backward_jump_allows_another_sample():
    respond = pixel_driver(
        times=(100, 99, 108), pixels=((0, 0, 0),), modes=(False,), heights=(600, 600)
    )
    trace, _ = drive(iter_legacy_foreground_pixels(), respond)
    assert sum(step.operation == "pixel" for step in trace) == 1
    assert not any(step == TraceRequest("sleep", (2,)) for step in trace)
    assert trace[-1] == TraceRequest("mouse_up")


def test_pixel_activation_precedes_geometry_and_properties_are_read_in_order():
    trace, _ = drive(iter_legacy_foreground_pixels(), pixel_driver(times=(0, 7)))
    assert trace[3:10] == [
        TraceRequest("read_window_title", (PixelWindow("w1"),)),
        TraceRequest("activate_title", ("window title",)),
        TraceRequest("sleep", (0.15,)),
        *(
            TraceRequest("read_window_property", (PixelWindow("w1"), name))
            for name in ("left", "top", "width", "height")
        ),
    ]


def test_pixel_failure_after_mouse_down_does_not_invent_mouse_up_or_later_windows():
    respond = pixel_driver(
        times=(0, 7), account_windows={"first": (PixelWindow("w1"), PixelWindow("w2"))}
    )
    observed = []

    def fail_after_down(step):
        observed.append(step)
        return (
            RuntimeError("hold_failed") if step == TraceRequest("sleep", (0.14,)) else respond(step)
        )

    with pytest.raises(RuntimeError, match="hold_failed"):
        drive(iter_legacy_foreground_pixels(), fail_after_down)
    assert observed[-2:] == [TraceRequest("mouse_down"), TraceRequest("sleep", (0.14,))]
    assert not any(step.operation == "mouse_up" for step in observed)


def test_title_activation_foreground_precedes_restore_even_for_zero_handle():
    trace, _ = drive(
        iter_legacy_title_activation("title"),
        lambda step: 0 if step.operation == "find_window" else None,
    )
    assert trace == [
        TraceRequest("find_window", (None, "title"), 1),
        TraceRequest("set_foreground_window", (0,), 1),
        TraceRequest("show_window", (0, "SW_RESTORE"), 1),
    ]


def test_title_activation_sleeps_after_third_failure_too():
    trace, _ = drive(
        iter_legacy_title_activation("title"),
        lambda step: RuntimeError() if step.operation == "find_window" else None,
    )
    assert (
        trace
        == [TraceRequest("find_window", (None, "title"), 1), TraceRequest("sleep", (0.2,), 1)] * 3
    )


@pytest.mark.parametrize("invalid", [True, "1", None, float("nan"), float("inf")])
def test_invalid_initial_numbers_fail_without_emitting_effects(invalid):
    with pytest.raises(AutoacceptPlanError, match="^autoaccept_plan_response_invalid$"):
        next(iter_legacy_dispatch(invalid, None))


def test_custom_values_are_not_coerced_compared_or_formatted():
    class Explosive:
        def __bool__(self):
            raise AssertionError("truthiness called")

        def __float__(self):
            raise AssertionError("float called")

        def __gt__(self, other):
            raise AssertionError("comparison called")

    sequence = iter_legacy_row_count(0)
    next(sequence)
    with pytest.raises(AutoacceptPlanError):
        sequence.send(rows(Explosive()))
    dispatcher = iter_legacy_dispatch(0, None)
    next(dispatcher)
    dispatcher.send(None)
    dispatcher.send(0)
    with pytest.raises(AutoacceptPlanError):
        dispatcher.send(Explosive())


def test_invalid_shuffle_cannot_add_or_drop_windows_and_is_not_swallowed():
    def respond(step):
        return WINDOWS[:1] if step.operation == "shuffle_windows" else batch_response(step)

    with pytest.raises(AutoacceptPlanError):
        drive(iter_legacy_background_batch(), respond)


def test_plans_are_immutable_and_close_emits_no_cleanup():
    sequence = iter_legacy_background_batch()
    step = next(sequence)
    with pytest.raises(FrozenInstanceError):
        setattr(step, "operation", "execute")
    sequence.close()
    assert list(sequence) == []


def test_normal_dispatcher_failure_is_terminal():
    def respond(step):
        if step.operation == "background_click_batch":
            return RuntimeError("normal_failure")
        return dispatcher_response(step)

    sequence = iter_legacy_dispatch(0, None)
    with pytest.raises(RuntimeError, match="normal_failure"):
        drive(sequence, respond)
    assert list(sequence) == []


def test_background_base_exception_skips_unblock_and_future_windows():
    observed = []

    def respond(step):
        observed.append(step)
        if step.operation == "post_message":
            return KeyboardInterrupt()
        return batch_response(step)

    sequence = iter_legacy_background_batch()
    with pytest.raises(KeyboardInterrupt):
        drive(sequence, respond)
    assert observed[-1].operation == "post_message"
    assert TraceRequest("block_input", (False,)) not in observed
    assert list(sequence) == []


def test_background_protocol_failure_is_not_legacy_dependency_recovery():
    observed = []

    def respond(step):
        observed.append(step)
        return 1 if step.operation == "read_setting" else batch_response(step)

    with pytest.raises(AutoacceptPlanError):
        drive(iter_legacy_background_batch(), respond)
    assert observed[-1].operation == "read_setting"
    assert TraceRequest("block_input", (False,)) not in observed


def test_title_activation_failure_during_retry_sleep_propagates():
    def respond(step):
        return RuntimeError("sleep_failed" if step.operation == "sleep" else "find_failed")

    with pytest.raises(RuntimeError, match="sleep_failed"):
        drive(iter_legacy_title_activation("title"), respond)
