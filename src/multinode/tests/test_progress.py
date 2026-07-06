"""Tests for the progress-callback plumbing."""

from __future__ import annotations

from dnlab_multinode.services.progress import (
    NullProgress, ProgressEvent, make_timer,
)


def test_null_progress_is_silent():
    # Must not raise, must return None.
    evt = ProgressEvent(phase="x", status="start")
    assert NullProgress(evt) is None


def test_timer_emits_start_and_ok_with_elapsed_ms(monkeypatch):
    events: list[ProgressEvent] = []
    timer = make_timer(events.append)

    # Fake time progression so elapsed_ms is deterministic.
    ticks = iter([100.0, 100.25])
    monkeypatch.setattr(
        "dnlab_multinode.services.progress.time.time",
        lambda: next(ticks),
    )

    timer.emit("deploy", "start", detail="starting")
    timer.emit("deploy", "ok", detail="done")

    assert [e.status for e in events] == ["start", "ok"]
    assert events[0].elapsed_ms == 0
    assert events[1].elapsed_ms == 250
    assert events[1].detail == "done"


def test_timer_tracks_per_host_phases():
    events: list[ProgressEvent] = []
    timer = make_timer(events.append)

    timer.emit("sync-image", "start", host="worker1", detail="t")
    timer.emit("sync-image", "start", host="worker2", detail="t")
    timer.emit("sync-image", "ok", host="worker1", detail="d")
    timer.emit("sync-image", "ok", host="worker2", detail="d")

    hosts = [e.host for e in events]
    assert hosts == ["worker1", "worker2", "worker1", "worker2"]


def test_callback_exception_does_not_propagate():
    def boom(_evt: ProgressEvent) -> None:
        raise RuntimeError("callback crashed")

    timer = make_timer(boom)
    # Must NOT raise — controllers should never die because a GUI handler
    # misbehaves.
    evt = timer.emit("deploy", "start")
    assert evt.phase == "deploy"
