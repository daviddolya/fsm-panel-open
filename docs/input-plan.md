# Recovered input fragments

`adapters/input_plan.py` describes one additional anti-AFK sequence and the key-hold
helper. It sends no keys, activates no windows, acquires no lock, sleeps for no time,
and starts no scheduler. It is usable for trace comparison and future driver tests.
This is a partial static contract, not a working anti-AFK or autoaccept integration.

The original is Panel.exe SHA-256
`3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308`.
Evidence is bounded read-only Ghidra/Capstone analysis, not target execution or
successful full native REA autoanalysis.

| Operation | Native evidence |
| --- | --- |
| Key hold `atyHdZCtnPLy`, source line 28180 | `0x140a39c00..0x140a3a6e1` |
| Global lock creation and assignment | `0x140c108a3`, `0x140c108cf` |
| Additional anti-AFK `xcauskXInZvu`, source line 28731 | `0x140a515c0` |
| Base timing and draws | `0x140a52e55`, `0x140a52fad`, `0x140a53140` |
| Late power read and inclusive threshold | `0x140a5324f`, `0x140a53286`; comparison helper `0x142d05880` |
| Additional delay/hold and final delay | `0x140a533a9`, `0x140a5353a`, `0x140a53659` |

The private sanitized checkpoint has SHA-256
`468d0c236ede14a5d971af2008fa5301d6824caca1d450ab505fb59c2cb1f0ac`.
Private dumps and unrelated constants are not included in this repository.

## Additional anti-AFK sequence

`iter_legacy_additional_afk()` yields requests for one already-eligible account:

1. Activate the account with the original third argument `False`. The original
   discards the activation helper's return value; this does not establish focus.
2. Delay 0.22 seconds.
3. Draw `uniform(0.15, 0.25)`; hold `f` for that duration.
4. Draw `uniform(0.8, 0.92)`; hold `ctrl` for that duration.
5. Read `ANTI_AFK_POWER` at this point. If it is at least 5, draw
   `uniform(0.3, 0.4)` and delay, then draw `uniform(0.7, 0.83)` and hold `ctrl`.
6. Delay 0.03 seconds.

The generator requests draws when reached and reads power after the first two
holds. It does not pre-sample values or snapshot power at activation. Send `None`
to acknowledge an activation, delay or hold, a numeric sample for `UniformDraw`,
and an exact integer for `ReadAntiAfkPower`. Closing the generator has no effects.
Throwing an exception ends the fragment; no further actions are returned.

Rejecting custom objects, non-finite or out-of-range samples, Boolean/float power
and unexpected acknowledgements is a new pure-API boundary. It does not claim
that the original validated those inputs. Power zero still yields the base
fragment: the outer eligibility/scheduling rules decide whether to invoke it.

This fragment is inside the original caller's `global_lock`. The generator owns
no such lock. The caller's broader exception handling, selection queue, cadence,
and the meanings of `ANTI_AFK_DELAY`/`ANTI_AFK_MINIMIZE` are outside this contract.

## Key hold and failure behavior

`build_legacy_key_hold(key, hold_time)` describes the complete helper:
acquire `global_lock`; format key with the empty format specifier; keyDown;
sleep; format the key again; keyUp; exit lock. The lock is an RLock and remains
held during sleep, including when reacquired inside the additional-AFK caller.

There is no original duration validation, cancellation check, retry, or guaranteed
keyUp after failure. Context cleanup exits the lock on exception, but does not
release the key. Negative/zero durations are retained in the descriptive plan.
Only inert strings and finite numbers are accepted by the new API.

A future real input driver must deliberately guarantee key release, cancellable
waits and ownership of the intended window. Those are new design requirements;
neither this plan nor synthetic tests prove successful input in CS2.

Run `python -m pytest tests/adapters/test_input_plan.py`. Tests exercise threshold
boundaries, independent draws, late reads, interruption at every step, terminal
failure, immutable key-hold plans and rejection of executable/custom values.
