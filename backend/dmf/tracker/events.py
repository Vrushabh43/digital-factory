"""Domain events emitted by the tracker. Pure data, no I/O.

Every event carries ``ts`` (epoch seconds, backend clock) and a plain-language
``message`` used for the "Latest events" list on the dashboard.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    ts: float
    kind: str = field(init=False, default="event")

    @property
    def message(self) -> str:  # pragma: no cover - overridden
        return self.kind

    @property
    def position(self) -> int | None:
        return getattr(self, "pos", None)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind
        d["message"] = self.message
        return d


@dataclass
class PieceAppeared(Event):
    code: str
    pos: int
    cell: int
    conveyor: int
    origin: str  # put_on | source:<name> | robot:<name> | found | startup
    uncertain: bool = False
    kind: str = field(init=False, default="piece_appeared")

    @property
    def message(self) -> str:
        if self.origin.startswith("source:"):
            return f"{self.code} came in from {self.origin.split(':', 1)[1]}"
        if self.origin == "found":
            return f"{self.code} found on conveyor {self.conveyor} after a data gap"
        return f"{self.code} was put on conveyor {self.conveyor}"


@dataclass
class CellEntered(Event):
    code: str
    pos: int
    cell: int
    conveyor: int
    came_from_pos: int | None
    came_from_cell: int | None
    via: str | None = None
    uncertain: bool = False
    kind: str = field(init=False, default="cell_entered")

    @property
    def message(self) -> str:
        return f"{self.code} entered at the entry sensor" if self.came_from_pos else f"{self.code} entered the cell"


@dataclass
class ConveyorChanged(Event):
    code: str
    pos: int
    from_conveyor: int
    to_conveyor: int
    kind: str = field(init=False, default="conveyor_changed")

    @property
    def message(self) -> str:
        return f"{self.code} moved to conveyor {self.to_conveyor}"


@dataclass
class ExitReached(Event):
    code: str
    pos: int
    conveyor: int
    kind: str = field(init=False, default="exit_reached")

    @property
    def message(self) -> str:
        return f"{self.code} reached the exit sensor"


@dataclass
class CellLeft(Event):
    code: str
    pos: int
    cell: int
    result: str  # passed | removed | stored | lost | to_robot
    to_pos: int | None = None
    uncertain: bool = False
    kind: str = field(init=False, default="cell_left")

    @property
    def message(self) -> str:
        return {
            "passed": f"{self.code} left at the exit sensor",
            "to_robot": f"{self.code} was picked up by the robot",
            "removed": f"{self.code} was taken off the line",
            "stored": f"{self.code} went into storage",
            "lost": f"{self.code} could not be followed any more",
        }.get(self.result, f"{self.code} left the cell")


@dataclass
class PieceEnded(Event):
    code: str
    status: str  # removed | stored | lost
    pos: int | None
    kind: str = field(init=False, default="piece_ended")

    @property
    def message(self) -> str:
        return {
            "removed": f"{self.code} was taken off the line",
            "stored": f"{self.code} left the factory into storage",
            "lost": f"{self.code} could not be followed any more",
        }[self.status]


@dataclass
class StuckChanged(Event):
    code: str
    pos: int
    stuck: bool
    since: float
    kind: str = field(init=False, default="stuck_changed")

    @property
    def message(self) -> str:
        if self.stuck:
            return f"{self.code} is waiting too long at the exit"
        return f"{self.code} is moving again"


@dataclass
class CellStatusChanged(Event):
    pos: int
    cell: int
    status: str  # running | stuck | error | offline
    previous: str | None
    reason: str
    kind: str = field(init=False, default="cell_status")

    @property
    def message(self) -> str:
        return {
            "running": "Cell is running",
            "stuck": "A piece is stuck in this cell",
            "error": "Cell reports an error (lamp red)",
            "offline": "No data from this cell",
        }[self.status]


@dataclass
class PlantFlagChanged(Event):
    flag: str  # alarm | enable
    value: bool | None
    kind: str = field(init=False, default="plant_flag")

    @property
    def message(self) -> str:
        if self.flag == "alarm":
            return "Plant alarm switched on" if self.value else "Plant alarm switched off"
        return "Plant enable switched on" if self.value else "Plant enable switched off"


@dataclass
class ConnectionChanged(Event):
    connected: bool
    reason: str
    kind: str = field(init=False, default="connection")

    @property
    def message(self) -> str:
        return "Connected to the factory" if self.connected else f"Connection to the factory lost ({self.reason})"


@dataclass
class Anomaly(Event):
    pos: int | None
    text: str
    code: str | None = None
    kind: str = field(init=False, default="anomaly")

    @property
    def message(self) -> str:
        return self.text
