# Refactor notes

## Why

`main.py` had grown to 668 lines because three unrelated concerns lived in the
same class: AstrBot compatibility shims, turn orchestration, and reusable
plumbing (watchdog timers, batch application, image de-duplication).

## Result

| Metric | Before | After |
| --- | --- | --- |
| `main.py` total lines | 668 | 442 |
| `main.py` code lines (no blank/comment/docstring) | 576 | 356 |
| `handle_interleave_intake` body | 100 | 39 |
| `inject_coalesce_context` body | 59 | 36 |
| `shape_outgoing_bubbles` body | 122 | 65 |
| `getattr(` occurrences in `main.py` | 41 | 0 |

`compat.py` now owns every reflection call; `coordinator.py` owns the watchdog
timers (see below); `api.py` is unchanged.

## Behavior fixes bundled with the refactor

1. **Single timeout authority.** `max_flight_seconds` is captured at admission
   and the coordinator arms exactly one timer per flight. Previously two
   mechanisms coexisted: the plugin's asyncio timer and a lazy check in
   `admit_or_coalesce`/`wait_and_collect_flush` that re-used the *arriving*
   event's policy, so a resolver returning a `BubblePolicy` with a shorter
   timeout could kill a turn that was already generating. The lazy check is
   gone; waiters now simply await the flight's `done_event`, which the timer
   sets on expiry.
2. **Lock-consistent rollback.** Cancelled waiters/idle leaders reset their
   state under the coordinator lock instead of mutating `_flights` from the
   `except BaseException` handler.
3. **No busy wait when the watchdog is disabled.** With
   `max_flight_seconds <= 0` the old wait loop recomputed a 50 ms timeout
   forever; it now waits for the release/close event without polling.

## Verification

```bash
pytest tests/test_bubble_flow.py -q   # 16 passed
```

The three new tests cover: an arrival policy must not shorten a running turn;
the coordinator emits exactly one expiry per stalled flight and none for a turn
that releases in time; a disabled watchdog still releases on normal finish.

## Known follow-ups

- `collapsed` is inferred from "raw lines > planned bubbles", so a
  `register_bubble_filter` that legally merges bubbles still reports
  `bubbles.collapsed`.
- `suppress_next_coalesce(umo)` and `set_session_policy(umo, ...)` keep
  per-session state without eviction.
- `AdmissionResult.mode` is still a bare string; module-level constants would
  make typos impossible without breaking Python 3.10 support.
