"""Inert descriptions of two recovered input operations; never sends input.

The additional-AFK generator represents one already-eligible account inside the
caller's global lock. Its driver supplies acknowledgements, random draws and the
late settings read. No scheduler, window choice, lock or random source is run.
"""

import math
from collections.abc import Generator
from dataclasses import dataclass
from typing import cast


class InputPlanError(ValueError):
    def __init__(self) -> None:
        super().__init__("input_plan_response_invalid")


@dataclass(frozen=True)
class ActivateAccount:
    # Other original arguments come from the enclosing account/window workflow.
    third_argument: bool = False


@dataclass(frozen=True)
class Delay:
    seconds: float


@dataclass(frozen=True)
class UniformDraw:
    lower: float
    upper: float


@dataclass(frozen=True)
class HoldKey:
    key: str
    seconds: float


@dataclass(frozen=True)
class ReadAntiAfkPower:
    setting: str = "ANTI_AFK_POWER"


type InputStep = ActivateAccount | Delay | UniformDraw | HoldKey | ReadAntiAfkPower


def _ack(value: object) -> None:
    if value is not None:
        raise InputPlanError()


def _draw(value: object, low: float, high: float) -> float:
    if type(value) not in (float, int):
        raise InputPlanError()
    number = cast(float | int, value)
    try:
        valid = math.isfinite(number) and low <= number <= high
    except OverflowError:
        valid = False
    if not valid:
        raise InputPlanError()
    return float(number)


def iter_legacy_additional_afk() -> Generator[InputStep, object]:
    """Yield the observed ordered requests, without executing them.

    Send None after an activation, delay or hold. Send an explicit numeric sample
    after UniformDraw and an exact integer after ReadAntiAfkPower. The activation
    result was discarded by the original: None here acknowledges completion, not
    successful focus. Inputs are intentionally narrower than Python's arbitrary
    objects. A thrown exception terminates this fragment; the outer caller's
    catch/continue policy and lock cleanup are not modeled by this generator.
    """
    _ack((yield ActivateAccount()))
    _ack((yield Delay(0.22)))
    duration = _draw((yield UniformDraw(0.15, 0.25)), 0.15, 0.25)
    _ack((yield HoldKey("f", duration)))
    duration = _draw((yield UniformDraw(0.8, 0.92)), 0.8, 0.92)
    _ack((yield HoldKey("ctrl", duration)))
    power = yield ReadAntiAfkPower()
    if type(power) is not int:
        raise InputPlanError()
    if power >= 5:
        duration = _draw((yield UniformDraw(0.3, 0.4)), 0.3, 0.4)
        _ack((yield Delay(duration)))
        duration = _draw((yield UniformDraw(0.7, 0.83)), 0.7, 0.83)
        _ack((yield HoldKey("ctrl", duration)))
    _ack((yield Delay(0.03)))


@dataclass(frozen=True)
class LegacyKeyHoldPlan:
    key_down: str
    sleep_seconds: float | int
    key_up: str
    lock: str = "global_lock"
    lock_held_during_sleep: bool = True
    lock_exit_on_exception: bool = True
    key_up_on_exception: bool = False
    formats_key_twice: bool = True
    cancellation_checks: bool = False


def build_legacy_key_hold(key: str, hold_time: float | int) -> LegacyKeyHoldPlan:
    """Describe format/keyDown/sleep/format/keyUp within the original RLock.

    Negative and zero durations remain unchanged, as no validation was present
    in the original. This API accepts only inert strings and finite numbers;
    no arbitrary __format__, sleeping, input, or lock operation is performed.
    The fields describe a legacy failure gap, not an approved input executor.
    """
    if type(key) is not str or type(hold_time) not in (int, float):
        raise InputPlanError()
    if type(hold_time) is float and not math.isfinite(hold_time):
        raise InputPlanError()
    return LegacyKeyHoldPlan(format(key, ""), hold_time, format(key, ""))
