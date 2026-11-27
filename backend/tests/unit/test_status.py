import pytest

from dmf.tracker.status import compute_status

BASE = dict(connected=True, lamp=3, lamp_good=True, stale=False, has_stuck_piece=False, plant_enabled=True)


@pytest.mark.parametrize(
    "changes,status,reason",
    [
        ({}, "running", "ok"),
        ({"lamp": 4}, "running", "ok"),
        ({"lamp": 2}, "running", "ok"),
        ({"connected": False}, "offline", "server_unreachable"),
        ({"lamp_good": False}, "offline", "no_valid_data"),
        ({"lamp": None}, "offline", "no_valid_data"),
        ({"stale": True}, "offline", "stale_data"),
        ({"lamp": 1}, "error", "lamp_red"),
        ({"lamp": 1, "plant_enabled": False}, "error", "plant_not_enabled"),
        ({"has_stuck_piece": True, "lamp": 2}, "stuck", "piece_waiting_too_long"),
        ({"has_stuck_piece": True, "lamp": 1}, "error", "lamp_red"),  # error wins over stuck
        ({"lamp": 9}, "offline", "unknown_lamp_value"),
    ],
)
def test_status_rules(changes, status, reason):
    res = compute_status(**{**BASE, **changes})
    assert (res.status, res.reason) == (status, reason)


def test_running_detail():
    assert compute_status(**{**BASE, "lamp": 2}).detail == "waiting"
    assert compute_status(**{**BASE, "lamp": 3}).detail == "idle"
    assert compute_status(**{**BASE, "lamp": 4}).detail == "transporting"
