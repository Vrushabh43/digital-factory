"""Cell status rules (see docs/02-architecture.md §2.6)."""

from __future__ import annotations

from dataclasses import dataclass

RUNNING, STUCK, ERROR, OFFLINE = "running", "stuck", "error", "offline"

# AmpelZustand values written by the PLC (PLC documentation, "Lampenansteuerung").
LAMP_ERROR = 1  # red steady: enable not set or OPC UA connection lost
LAMP_WAITING = 2  # yellow blinking: waiting at an output port
LAMP_IDLE = 3  # green blinking: idle, waiting for a piece
LAMP_TRANSPORTING = 4  # green steady: transporting

LAMP_DETAIL = {LAMP_WAITING: "waiting", LAMP_IDLE: "idle", LAMP_TRANSPORTING: "transporting"}


@dataclass(frozen=True)
class StatusResult:
    status: str
    reason: str  # machine-readable reason code
    detail: str | None = None  # running sub-state: waiting | idle | transporting


def compute_status(
    *,
    connected: bool,
    lamp: int | None,
    lamp_good: bool,
    stale: bool,
    has_stuck_piece: bool,
    plant_enabled: bool | None,
    partial: bool = False,
) -> StatusResult:
    """``partial``: the lamp is fine but a conveyor of the cell has no valid state (Bad
    quality or an invalid value), so pieces in this cell may not be tracked correctly."""
    if not connected:
        return StatusResult(OFFLINE, "server_unreachable")
    if not lamp_good or lamp is None:
        return StatusResult(OFFLINE, "no_valid_data")
    if stale:
        return StatusResult(OFFLINE, "stale_data")
    if lamp == LAMP_ERROR:
        return StatusResult(ERROR, "plant_not_enabled" if plant_enabled is False else "lamp_red")
    if has_stuck_piece:
        return StatusResult(STUCK, "piece_waiting_too_long", LAMP_DETAIL.get(lamp))
    if lamp in LAMP_DETAIL:
        return StatusResult(RUNNING, "partial_data" if partial else "ok", LAMP_DETAIL[lamp])
    return StatusResult(OFFLINE, "unknown_lamp_value")
