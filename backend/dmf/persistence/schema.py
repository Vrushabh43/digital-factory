"""Database schema (SQLAlchemy Core). Migrations live in database/migrations (Alembic).

Times are stored as ``timestamptz`` (UTC). Positions are blueprint positions 1..18;
``cell_number`` is the physical cell number shown on the dashboard.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = DateTime(timezone=True)

pieces = Table(
    "pieces",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("code", String(16), nullable=False, unique=True),
    Column("seq", Integer, nullable=False, unique=True),
    Column("first_seen_at", TS, nullable=False),
    Column("first_position", SmallInteger, nullable=False),
    Column("first_cell", SmallInteger, nullable=False),
    Column("origin", String(64), nullable=False),  # put_on | source:<name> | robot:<name> | startup | found
    Column("status", String(16), nullable=False, server_default="active"),  # active | removed | stored | lost
    Column("ended_at", TS),
    Column("last_position", SmallInteger),
    Column("last_seen_at", TS, nullable=False),
    Column("cells_passed", Integer, nullable=False, server_default="0"),
    Column("uncertain", Boolean, nullable=False, server_default=text("false")),
    Index("ix_pieces_status", "status"),
    Index("ix_pieces_first_seen_at", "first_seen_at"),
)

cell_passes = Table(
    "cell_passes",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("piece_id", Integer, ForeignKey("pieces.id", ondelete="CASCADE"), nullable=False),
    Column("position", SmallInteger, nullable=False),
    Column("cell_number", SmallInteger, nullable=False),
    Column("entered_at", TS, nullable=False),
    Column("left_at", TS),
    # inside | passed | to_robot | removed | stored | lost
    Column("result", String(16), nullable=False, server_default="inside"),
    Column("was_stuck", Boolean, nullable=False, server_default=text("false")),
    Column("stuck_since", TS),
    Column("came_from_position", SmallInteger),
    Column("came_from_cell", SmallInteger),
    Column("went_to_position", SmallInteger),
    Column("via", String(32)),
    Column("first_conveyor", SmallInteger),
    Column("last_conveyor", SmallInteger),
    Column("uncertain", Boolean, nullable=False, server_default=text("false")),
    Index("ix_cell_passes_entered_at", "entered_at"),
    Index("ix_cell_passes_position_entered_at", "position", "entered_at"),
    Index("ix_cell_passes_piece_id", "piece_id"),
    # at most one open pass per piece
    Index("uq_cell_passes_open_piece", "piece_id", unique=True, postgresql_where=text("left_at IS NULL")),
)

events = Table(
    "events",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("ts", TS, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("position", SmallInteger),
    Column("cell_number", SmallInteger),
    Column("piece_code", String(16)),
    Column("message", Text, nullable=False),
    Column("details", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Index("ix_events_ts", "ts"),
    Index("ix_events_position_ts", "position", "ts"),
    Index("ix_events_piece_code", "piece_code"),
)

cell_status_history = Table(
    "cell_status_history",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("position", SmallInteger, nullable=False),
    Column("cell_number", SmallInteger, nullable=False),
    Column("status", String(16), nullable=False),  # running | stuck | error | offline
    Column("reason", String(48), nullable=False),
    Column("since", TS, nullable=False),
    Column("until", TS),
    Index("ix_cell_status_history_position_since", "position", "since"),
    Index("uq_cell_status_history_open", "position", unique=True, postgresql_where=text("until IS NULL")),
)

tracker_snapshot = Table(
    "tracker_snapshot",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),  # always 1
    Column("saved_at", TS, nullable=False),
    Column("data", JSONB, nullable=False),
)

# Replay protection for the event writer: every write transaction records the sequence
# number of its last item (per backend process = "epoch") in the same transaction. After
# an ambiguous failure (e.g. the COMMIT reply was lost) the writer reads this row and does
# not apply already committed items a second time.
writer_progress = Table(
    "writer_progress",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),  # always 1
    Column("epoch", String(36), nullable=False),
    Column("last_seq", BigInteger, nullable=False),
    Column("updated_at", TS, nullable=False),
)
