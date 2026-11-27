"""Event writer: error classification and the bounded in-memory buffer (no database)."""

from __future__ import annotations

from sqlalchemy.exc import DataError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from dmf.persistence.writer import EventWriter, _Snapshot, is_data_error
from dmf.tracker import events as ev


def _pg(cls, sqlstate: str):
    class Orig(Exception):
        pass

    o = Orig("x")
    o.sqlstate = sqlstate  # type: ignore[attr-defined]
    return cls("SQL", {}, o)


def test_only_data_and_integrity_errors_count_as_bad_data():
    assert is_data_error(_pg(DataError, "22001"))  # value too long
    assert is_data_error(_pg(IntegrityError, "23505"))  # unique violation
    assert is_data_error(KeyError("missing field"))  # Python error while building rows
    # everything else must be retried, never skipped
    for retry in (
        _pg(OperationalError, "57P03"),  # database starting up / shutting down
        _pg(OperationalError, "53100"),  # disk full
        _pg(OperationalError, "25006"),  # read-only transaction
        _pg(OperationalError, "57014"),  # statement timeout
        _pg(ProgrammingError, "42P01"),  # schema not migrated yet
        PoolTimeout("QueuePool limit reached"),
        ConnectionResetError(),
        TimeoutError(),
    ):
        assert not is_data_error(retry), retry


def _anomaly(i: int) -> ev.Event:
    return ev.Anomaly(float(i), None, f"a{i}")


def _texts(w: EventWriter) -> list[str]:
    return [e.item.text if isinstance(e.item, ev.Anomaly) else "snap" for e in w._buf]


def test_full_buffer_drops_oldest_item_that_is_not_being_written():
    w = EventWriter(None, None, buffer_max=4)  # type: ignore[arg-type]
    w.submit([_anomaly(i) for i in range(4)])
    w._inflight = 2  # a transaction is writing a0, a1
    w.submit([_anomaly(4), _anomaly(5)])
    assert _texts(w) == ["a0", "a1", "a4", "a5"]  # a2, a3 dropped; in-flight items kept
    assert w.status.dropped == 2 and w.status.pending == 4
    w._done(2, written=True)
    assert _texts(w) == ["a4", "a5"] and w.status.written == 2


def test_only_the_newest_pending_snapshot_is_kept():
    w = EventWriter(None, None)  # type: ignore[arg-type]
    w.submit([_anomaly(1)])
    w.submit_snapshot({"v": 1}, 1.0)
    w.submit([_anomaly(2)])
    w.submit_snapshot({"v": 2}, 2.0)
    assert _texts(w) == ["a1", "a2", "snap"]
    assert isinstance(w._buf[-1].item, _Snapshot) and w._buf[-1].item.data == {"v": 2}
    seqs = [e.seq for e in w._buf]
    assert seqs == sorted(seqs)  # order is preserved
