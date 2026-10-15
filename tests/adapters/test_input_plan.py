from dataclasses import FrozenInstanceError

import pytest

from fsm_panel.adapters.input_plan import (
    ActivateAccount,
    Delay,
    HoldKey,
    InputPlanError,
    ReadAntiAfkPower,
    UniformDraw,
    build_legacy_key_hold,
    iter_legacy_additional_afk,
)


def drive(power, samples):
    sequence = iter_legacy_additional_afk()
    samples = iter(samples)
    trace = []
    step = next(sequence)
    while True:
        trace.append(step)
        response = power if isinstance(step, ReadAntiAfkPower) else None
        if isinstance(step, UniformDraw):
            response = next(samples)
        try:
            step = sequence.send(response)
        except StopIteration:
            return trace


@pytest.mark.parametrize("power", [-10, 0, 4])
def test_low_power_keeps_base_holds_and_reads_setting_after_effects(power):
    trace = drive(power, [0.19, 0.87])
    assert trace == [
        ActivateAccount(False),
        Delay(0.22),
        UniformDraw(0.15, 0.25),
        HoldKey("f", 0.19),
        UniformDraw(0.8, 0.92),
        HoldKey("ctrl", 0.87),
        ReadAntiAfkPower(),
        Delay(0.03),
    ]


@pytest.mark.parametrize("power", [5, 6, 10**100])
def test_inclusive_threshold_requests_two_more_independent_draws(power):
    trace = drive(power, [0.25, 0.8, 0.4, 0.7])
    assert trace[7:] == [
        UniformDraw(0.3, 0.4),
        Delay(0.4),
        UniformDraw(0.7, 0.83),
        HoldKey("ctrl", 0.7),
        Delay(0.03),
    ]
    assert sum(isinstance(step, UniformDraw) for step in trace) == 4


@pytest.mark.parametrize("failure_index", range(12))
def test_failure_at_each_step_terminates_fragment_without_later_effects(failure_index):
    sequence = iter_legacy_additional_afk()
    step = next(sequence)
    draws = iter([0.2, 0.9, 0.35, 0.8])
    trace = []
    for _ in range(failure_index):
        trace.append(step)
        response = 5 if isinstance(step, ReadAntiAfkPower) else None
        if isinstance(step, UniformDraw):
            response = next(draws)
        step = sequence.send(response)
    with pytest.raises(RuntimeError, match="synthetic_failure"):
        sequence.throw(RuntimeError("synthetic_failure"))
    assert list(sequence) == []
    if failure_index <= 5:
        assert not any(isinstance(item, ReadAntiAfkPower) for item in trace)


@pytest.mark.parametrize(
    "value", [None, True, "0.2", float("nan"), float("inf"), 0.149, 0.251, 10**400]
)
def test_invalid_first_draw_never_emits_key_hold(value):
    sequence = iter_legacy_additional_afk()
    assert next(sequence) == ActivateAccount()
    assert sequence.send(None) == Delay(0.22)
    assert sequence.send(None) == UniformDraw(0.15, 0.25)
    with pytest.raises(InputPlanError, match="^input_plan_response_invalid$"):
        sequence.send(value)
    assert list(sequence) == []


@pytest.mark.parametrize("value", [None, True, "5", 5.0, [], {}])
def test_power_is_explicit_inert_integer(value):
    with pytest.raises(InputPlanError):
        drive(value, [0.2, 0.9])


def test_custom_objects_are_never_coerced_or_formatted():
    class Explosive:
        def __format__(self, spec):
            raise AssertionError("custom format called")

        def __float__(self):
            raise AssertionError("custom float called")

        def __ge__(self, other):
            raise AssertionError("custom comparison called")

    with pytest.raises(InputPlanError):
        build_legacy_key_hold(Explosive(), 1)
    with pytest.raises(InputPlanError):
        build_legacy_key_hold("f", Explosive())
    with pytest.raises(InputPlanError):
        drive(Explosive(), [0.2, 0.9])


def test_bad_acknowledgement_cannot_silently_advance_to_input():
    sequence = iter_legacy_additional_afk()
    next(sequence)
    with pytest.raises(InputPlanError):
        sequence.send(True)
    assert list(sequence) == []


@pytest.mark.parametrize("duration", [-5, 0, 0.3, 10**400])
def test_hold_plan_preserves_no_validation_and_release_gap(duration):
    plan = build_legacy_key_hold("ctrl", duration)
    assert (plan.key_down, plan.sleep_seconds, plan.key_up) == ("ctrl", duration, "ctrl")
    assert plan.lock == "global_lock"
    assert plan.lock_held_during_sleep and plan.lock_exit_on_exception
    assert plan.formats_key_twice and not plan.key_up_on_exception
    assert not plan.cancellation_checks
    with pytest.raises(FrozenInstanceError):
        plan.key_up = "f"


@pytest.mark.parametrize("duration", [True, None, "1", float("nan"), float("inf")])
def test_hold_plan_rejects_unsafe_input_types(duration):
    with pytest.raises(InputPlanError):
        build_legacy_key_hold("f", duration)


def test_closing_pending_plan_has_no_effect_or_later_steps():
    sequence = iter_legacy_additional_afk()
    assert next(sequence) == ActivateAccount()
    sequence.close()
    assert list(sequence) == []
