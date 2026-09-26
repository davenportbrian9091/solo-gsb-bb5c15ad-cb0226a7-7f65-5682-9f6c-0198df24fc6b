"""Concurrent-span windows for one trace.

The stored spans are half-open nanosecond intervals ``[start_ns, end_ns)``.
A sweep line over their endpoints tracks how many spans are active at each
instant and reports the merged time ranges where that count reaches a
threshold (at least two by default).

Half-open semantics mean a span ending at *t* is no longer active at *t* and
a span starting at *t* becomes active at *t*: endpoints coinciding that way
produce no spurious overlap, so at a shared timestamp end events are applied
before start events. Zero-length spans (``start_ns == end_ns``) never occupy
any instant and are ignored entirely. Adjacent windows (the active count
dips below the threshold only at a single instant) stay merged, since no
positive time passes between them.

Pure functions only — no I/O, no sampling policy.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


def concurrency_windows(
    rows: Iterable[Mapping[str, Any]], min_active: int = 2
) -> dict[str, Any]:
    """Find the merged windows where at least ``min_active`` spans overlap.

    ``rows`` are mappings with at least ``start_ns`` and ``end_ns`` keys
    (``sqlite3.Row`` works). Returns
    ``{"span_count", "window_count", "total_ns", "max_active", "windows"}``
    where each window is
    ``{"start_ns", "end_ns", "peak_active", "duration_ns"}`` and windows are
    ordered by start time. Zero-length spans are included in ``span_count``
    but never in the overlap computation.
    """
    all_rows = list(rows)
    intervals = sorted(
        (int(r["start_ns"]), int(r["end_ns"]))
        for r in all_rows
        if int(r["end_ns"]) > int(r["start_ns"])
    )

    # (time, delta): -1 ends before +1 starts at the same timestamp, so a
    # span ending and another starting at one instant never overlap there.
    events: list[tuple[int, int]] = []
    for start_ns, end_ns in intervals:
        events.append((start_ns, 1))
        events.append((end_ns, -1))
    events.sort(key=lambda e: (e[0], e[1]))

    windows: list[dict[str, Any]] = []
    active = 0
    max_active = 0
    prev_time: int | None = None
    seg_start: int | None = None
    seg_peak = 0
    total_ns = 0

    for time_ns, delta in events:
        # The slab [prev_time, time_ns) elapsed with ``active`` spans live.
        # Slabs of zero length (several endpoints at one timestamp) are
        # skipped, transparently merging abutting qualifying slabs.
        if prev_time is not None and time_ns > prev_time:
            if active >= min_active:
                if seg_start is None:
                    seg_start = prev_time
                    seg_peak = active
                elif active > seg_peak:
                    seg_peak = active
            elif seg_start is not None:
                windows.append(_window(seg_start, prev_time, seg_peak))
                total_ns += prev_time - seg_start
                seg_start = None
                seg_peak = 0
        active += delta
        if active > max_active:
            max_active = active
        prev_time = time_ns

    if seg_start is not None:
        assert prev_time is not None
        windows.append(_window(seg_start, prev_time, seg_peak))
        total_ns += prev_time - seg_start

    return {
        "span_count": len(all_rows),
        "window_count": len(windows),
        "total_ns": total_ns,
        "max_active": max_active,
        "windows": windows,
    }


def _window(start_ns: int, end_ns: int, peak_active: int) -> dict[str, Any]:
    return {
        "start_ns": start_ns,
        "end_ns": end_ns,
        "peak_active": peak_active,
        "duration_ns": end_ns - start_ns,
    }
