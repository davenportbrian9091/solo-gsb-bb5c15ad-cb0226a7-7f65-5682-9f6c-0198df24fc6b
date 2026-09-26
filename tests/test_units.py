"""Unit tests for timestamp normalization and stable hashing."""
from __future__ import annotations

import pytest

from app.timeutil import minute_bucket, to_ns
from app.sampler import hash_score, select_by_hash, stable_hash
from app.servicetime import exclusive_by_service, _union_length
from app.concurrency import concurrency_windows
from app.models import SpanIn
from pydantic import ValidationError


def _row(span_id, start_ns, end_ns, *, parent=None, service="svc"):
    return {
        "span_id": span_id,
        "parent_span_id": parent,
        "service": service,
        "start_ns": start_ns,
        "end_ns": end_ns,
    }


def _by_service(rows):
    return {e["service"]: e for e in exclusive_by_service(rows)}


def test_union_length_merges_overlapping_and_abutting():
    assert _union_length([]) == 0
    assert _union_length([(10, 40)]) == 30
    assert _union_length([(10, 40), (30, 80)]) == 70
    assert _union_length([(0, 10), (10, 20)]) == 20
    assert _union_length([(0, 10), (20, 30)]) == 20
    # Identical interval counts once.
    assert _union_length([(0, 50), (0, 50)]) == 50


def test_exclusive_time_subtracts_direct_children():
    rows = [
        _row("root", 0, 100, parent=None),
        _row("child", 20, 60, parent="root"),
    ]
    svcs = _by_service(rows)
    assert svcs["svc"]["span_count"] == 2
    # root: 100 - 40 = 60; child: 40 leaf time.
    assert svcs["svc"]["exclusive_ns"] == 100


def test_overlapping_children_are_subtracted_only_once():
    rows = [
        _row("root", 0, 100),
        _row("a", 10, 40, parent="root"),
        _row("b", 30, 80, parent="root"),
    ]
    # union [10,80) = 70 -> root exclusive 30; leaves add 30 + 50 = 80.
    assert _by_service(rows)["svc"]["exclusive_ns"] == 110


def test_child_extending_beyond_parent_is_clipped_to_parent():
    rows = [
        _row("root", 10, 50),
        _row("child", 0, 100, parent="root"),
    ]
    # root exclusive 0; child exclusive 100.
    assert _by_service(rows)["svc"]["exclusive_ns"] == 100


def test_self_referencing_parent_is_ignored():
    rows = [_row("loop", 0, 30, parent="loop")]
    assert _by_service(rows)["svc"]["exclusive_ns"] == 30


def test_missing_parent_is_not_a_child_of_anything():
    # Orphan span pointing at a span that never arrived keeps full time.
    rows = [_row("orphan", 0, 40, parent="absent-parent")]
    assert _by_service(rows)["svc"]["exclusive_ns"] == 40


def test_zero_duration_span_contributes_zero():
    rows = [
        _row("root", 0, 100),
        _row("instant", 50, 50, parent="root"),
    ]
    assert _by_service(rows)["svc"]["exclusive_ns"] == 100


def test_missing_service_lands_in_null_group():
    rows = [
        _row("a", 0, 30, service=None),
        _row("b", 0, 20, service="svc"),
    ]
    entries = {e["service"]: e for e in exclusive_by_service(rows)}
    assert entries[None] == {
        "service": None, "span_count": 1, "exclusive_ns": 30
    }
    assert entries["svc"]["exclusive_ns"] == 20


def test_sorting_ns_desc_name_asc_null_last():
    rows = [
        _row("b1", 0, 20, service="zsvc"),     # exclusive 20
        _row("b2", 0, 20, service="asvc"),     # exclusive 20, name tie-break
        _row("c", 0, 100, service="msvc"),     # exclusive 100
        _row("n", 0, 20, service=None),        # null, last among the 20s
    ]
    result = exclusive_by_service(rows)
    assert [(e["service"], e["exclusive_ns"]) for e in result] == [
        ("msvc", 100), ("asvc", 20), ("zsvc", 20), (None, 20),
    ]


def test_nested_spans_exclusive_sums_to_wall_clock_length():
    rows = [
        _row("root", 0, 100, service="a"),
        _row("mid", 10, 90, parent="root", service="b"),
        _row("leaf", 20, 80, parent="mid", service="c"),
    ]
    # a:20, b:20, c:60 -> total 100, the trace length.
    result = _by_service(rows)
    assert result["a"]["exclusive_ns"] == 20
    assert result["b"]["exclusive_ns"] == 20
    assert result["c"]["exclusive_ns"] == 60


def test_epoch_units_are_normalized_to_ns():
    assert to_ns(1_700_000_000) == 1_700_000_000 * 10**9
    assert to_ns(1_700_000_000_000) == 1_700_000_000 * 10**9
    assert to_ns(1_700_000_000_000_000) == 1_700_000_000 * 10**9
    assert to_ns(1_700_000_000_000_000_000) == 1_700_000_000 * 10**9
    assert to_ns(1_700_000_000.5) == 1_700_000_000 * 10**9 + 500_000_000


def test_integer_epoch_ns_round_trips_exactly():
    # Values above 2**53 are not all representable as float; integer input
    # must not be routed through float on the way out.
    odd_ns = 1_577_840_400_010_000_001
    assert to_ns(odd_ns) == odd_ns


def test_iso8601_z_and_offsets():
    assert to_ns("2023-11-14T22:13:20Z") == 1_700_000_000 * 10**9
    assert to_ns("2023-11-14T23:13:20+01:00") == 1_700_000_000 * 10**9
    # Naive timestamps are interpreted as UTC.
    assert to_ns("2023-11-14T22:13:20") == 1_700_000_000 * 10**9


@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-99", None])
def test_bad_timestamps_raise(bad):
    with pytest.raises((ValueError, TypeError)):
        to_ns(bad)


def test_minute_bucket_aligns_downward():
    base = 1_700_000_040  # epoch seconds, known multiple of 60
    assert minute_bucket((base + 59) * 10**9) == base
    assert minute_bucket((base + 60) * 10**9) == base + 60


def test_hash_is_stable_and_spread():
    assert stable_hash("abc") == stable_hash("abc")
    assert stable_hash("abc") != stable_hash("abd")
    scores = [hash_score(f"trace-{i}") for i in range(2000)]
    assert min(scores) >= 0 and max(scores) < 1
    # Rough uniformity: roughly half on each side of 0.5.
    below = sum(1 for s in scores if s < 0.5)
    assert 800 < below < 1200


def test_span_invalid_timestamp_is_rejected():
    with pytest.raises(ValidationError):
        SpanIn(trace_id="t", span_id="s", start_time="nope", end_time=1)
    # end before start is schema-valid but rejected per-span by the engine.
    span = SpanIn(trace_id="t", span_id="s", start_time=10, end_time=5)
    assert span.trace_id == "t"


def test_select_by_hash_exact_counts_and_stability():
    ids = [f"t{i}" for i in range(20)]
    kept1, dropped1 = select_by_hash(ids, 7)
    kept2, dropped2 = select_by_hash(list(reversed(ids)), 7)
    assert len(kept1) == 7 and len(dropped1) == 13
    # Input order must not change which traces win.
    assert set(kept1) == set(kept2)
    assert set(dropped1) == set(dropped2)
    # Zero budget drops everyone; oversized budget keeps everyone.
    kept, dropped = select_by_hash(ids, 0)
    assert kept == [] and len(dropped) == 20
    kept, dropped = select_by_hash(ids, 100)
    assert len(kept) == 20 and dropped == []


def _iv(rows):
    return [{"start_ns": a, "end_ns": b} for a, b in rows]


def test_concurrency_basic_overlap_window():
    result = concurrency_windows(_iv([(0, 10), (5, 20)]))
    assert result["window_count"] == 1
    assert result["total_ns"] == 5
    assert result["max_active"] == 2
    assert result["windows"] == [
        {"start_ns": 5, "end_ns": 10, "peak_active": 2, "duration_ns": 5}
    ]


def test_concurrency_end_start_touch_does_not_overlap():
    # Half-open [start,end): the second starts exactly when the first ends.
    assert concurrency_windows(_iv([(0, 10), (10, 20)]))["windows"] == []
    # Even with a third span, the touching point only stays (or dips) —
    # active is 1 at t=10 but never 2 there.
    result = concurrency_windows(_iv([(0, 10), (10, 20), (10, 30)]))
    assert result["windows"] == [
        {"start_ns": 10, "end_ns": 20, "peak_active": 2, "duration_ns": 10}
    ]


def test_concurrency_adjacent_windows_merge_through_instant_dip():
    # A,B overlap on [0,10); B continues and C starts at t=10, overlapping on
    # [10,20). Active is below 2 only at the single instant t=10 -> merged.
    result = concurrency_windows(_iv([(0, 10), (0, 20), (10, 20)]))
    assert result["window_count"] == 1
    assert result["windows"] == [
        {"start_ns": 0, "end_ns": 20, "peak_active": 2, "duration_ns": 20}
    ]
    assert result["total_ns"] == 20


def test_concurrency_positive_gap_splits_windows():
    result = concurrency_windows(
        _iv([(0, 10), (5, 10), (15, 20), (15, 25)])
    )
    assert result["window_count"] == 2
    assert result["total_ns"] == 10
    assert [w["start_ns"] for w in result["windows"]] == [5, 15]


def test_concurrency_nested_spans_track_peak():
    result = concurrency_windows(_iv([(0, 100), (10, 90), (20, 80)]))
    assert result["max_active"] == 3
    # Qualifying at depth >= 2 from t=10; peak 3 on the nested core.
    assert result["windows"] == [
        {"start_ns": 10, "end_ns": 90, "peak_active": 3, "duration_ns": 80}
    ]


def test_concurrency_peak_is_max_across_merged_window():
    # Two overlap regions joined at an instant: peaks 3 then 2.
    result = concurrency_windows(
        _iv([(0, 10), (0, 10), (0, 5), (10, 20), (10, 20)])
    )
    assert result["window_count"] == 1
    assert result["windows"][0]["start_ns"] == 0
    assert result["windows"][0]["end_ns"] == 20
    assert result["windows"][0]["peak_active"] == 3
    assert result["total_ns"] == 20


def test_concurrency_zero_length_spans_are_ignored():
    result = concurrency_windows(_iv([(0, 0), (0, 10), (10, 20), (20, 20)]))
    assert result["span_count"] == 4
    assert result["max_active"] == 1
    assert result["windows"] == []
    assert concurrency_windows(_iv([(5, 5)]))["windows"] == []


def test_concurrency_custom_threshold():
    rows = _iv([(0, 10), (2, 8)])
    assert concurrency_windows(rows, min_active=2)["window_count"] == 1
    assert concurrency_windows(rows, min_active=3)["windows"] == []
    assert concurrency_windows(rows, min_active=3)["max_active"] == 2


def test_concurrency_empty_and_sorted_output():
    result = concurrency_windows([])
    assert result == {
        "span_count": 0, "window_count": 0, "total_ns": 0,
        "max_active": 0, "windows": [],
    }
    # Input order does not matter; windows come back ordered by start.
    shuffled = _iv([(30, 40), (0, 10), (30, 45), (0, 8)])
    starts = [w["start_ns"] for w in concurrency_windows(shuffled)["windows"]]
    assert starts == sorted(starts) == [0, 30]
