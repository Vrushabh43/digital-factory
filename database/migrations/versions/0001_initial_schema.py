"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-26 17:14:16.176551+00:00
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cell_status_history',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('position', sa.SmallInteger(), nullable=False),
    sa.Column('cell_number', sa.SmallInteger(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=48), nullable=False),
    sa.Column('since', sa.DateTime(timezone=True), nullable=False),
    sa.Column('until', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cell_status_history'))
    )
    op.create_index('ix_cell_status_history_position_since', 'cell_status_history', ['position', 'since'], unique=False)
    op.create_index('uq_cell_status_history_open', 'cell_status_history', ['position'], unique=True, postgresql_where=sa.text('until IS NULL'))
    op.create_table('events',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('position', sa.SmallInteger(), nullable=True),
    sa.Column('cell_number', sa.SmallInteger(), nullable=True),
    sa.Column('piece_code', sa.String(length=16), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_events'))
    )
    op.create_index('ix_events_piece_code', 'events', ['piece_code'], unique=False)
    op.create_index('ix_events_position_ts', 'events', ['position', 'ts'], unique=False)
    op.create_index('ix_events_ts', 'events', ['ts'], unique=False)
    op.create_table('pieces',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=16), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('first_position', sa.SmallInteger(), nullable=False),
    sa.Column('first_cell', sa.SmallInteger(), nullable=False),
    sa.Column('origin', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_position', sa.SmallInteger(), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('cells_passed', sa.Integer(), server_default='0', nullable=False),
    sa.Column('uncertain', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pieces')),
    sa.UniqueConstraint('code', name=op.f('uq_pieces_code')),
    sa.UniqueConstraint('seq', name=op.f('uq_pieces_seq'))
    )
    op.create_index('ix_pieces_first_seen_at', 'pieces', ['first_seen_at'], unique=False)
    op.create_index('ix_pieces_status', 'pieces', ['status'], unique=False)
    op.create_table('tracker_snapshot',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('saved_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tracker_snapshot'))
    )
    op.create_table('cell_passes',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('piece_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.SmallInteger(), nullable=False),
    sa.Column('cell_number', sa.SmallInteger(), nullable=False),
    sa.Column('entered_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('left_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', sa.String(length=16), server_default='inside', nullable=False),
    sa.Column('was_stuck', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('stuck_since', sa.DateTime(timezone=True), nullable=True),
    sa.Column('came_from_position', sa.SmallInteger(), nullable=True),
    sa.Column('came_from_cell', sa.SmallInteger(), nullable=True),
    sa.Column('went_to_position', sa.SmallInteger(), nullable=True),
    sa.Column('via', sa.String(length=32), nullable=True),
    sa.Column('first_conveyor', sa.SmallInteger(), nullable=True),
    sa.Column('last_conveyor', sa.SmallInteger(), nullable=True),
    sa.Column('uncertain', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.ForeignKeyConstraint(['piece_id'], ['pieces.id'], name=op.f('fk_cell_passes_piece_id_pieces'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cell_passes'))
    )
    op.create_index('ix_cell_passes_entered_at', 'cell_passes', ['entered_at'], unique=False)
    op.create_index('ix_cell_passes_piece_id', 'cell_passes', ['piece_id'], unique=False)
    op.create_index('ix_cell_passes_position_entered_at', 'cell_passes', ['position', 'entered_at'], unique=False)
    op.create_index('uq_cell_passes_open_piece', 'cell_passes', ['piece_id'], unique=True, postgresql_where=sa.text('left_at IS NULL'))


def downgrade() -> None:
    op.drop_index('uq_cell_passes_open_piece', table_name='cell_passes', postgresql_where=sa.text('left_at IS NULL'))
    op.drop_index('ix_cell_passes_position_entered_at', table_name='cell_passes')
    op.drop_index('ix_cell_passes_piece_id', table_name='cell_passes')
    op.drop_index('ix_cell_passes_entered_at', table_name='cell_passes')
    op.drop_table('cell_passes')
    op.drop_table('tracker_snapshot')
    op.drop_index('ix_pieces_status', table_name='pieces')
    op.drop_index('ix_pieces_first_seen_at', table_name='pieces')
    op.drop_table('pieces')
    op.drop_index('ix_events_ts', table_name='events')
    op.drop_index('ix_events_position_ts', table_name='events')
    op.drop_index('ix_events_piece_code', table_name='events')
    op.drop_table('events')
    op.drop_index('uq_cell_status_history_open', table_name='cell_status_history', postgresql_where=sa.text('until IS NULL'))
    op.drop_index('ix_cell_status_history_position_since', table_name='cell_status_history')
    op.drop_table('cell_status_history')
