"""Reconnect back-off: a session that drops right after connecting must not reset it."""

import asyncio

from dmf.opcua.ingest import IngestStatus, OpcIngest
from dmf.settings import Settings

MIN_S, MAX_S = 0.01, 0.08


class _Client:
    async def disconnect(self):
        pass


async def _waits(plant, lasting_s: float, attempts: int = 5) -> list[float]:
    """Run the reconnect loop with sessions that connect, last ``lasting_s`` (fake clock) and drop.
    Returns the waiting time the loop chose before each retry."""
    now = [1000.0]
    waits: list[float] = []

    class RecordingStatus(IngestStatus):
        def __setattr__(self, key, value):
            if key == "next_retry_at" and value is not None:
                waits.append(value - now[0])
            super().__setattr__(key, value)

    s = Settings(_env_file=None, opcua_reconnect_min_s=MIN_S, opcua_reconnect_max_s=MAX_S)
    ing = OpcIngest(plant, s, asyncio.Queue(), clock=lambda: now[0], client_factory=_Client)
    ing.status = RecordingStatus(endpoint="opc.tcp://mock")

    async def session(client, reason):
        if len(waits) >= attempts:
            ing._stop.set()
            return
        ing.status.state = "connected"
        ing.status.connected_since = now[0]
        now[0] += lasting_s
        raise ConnectionError("dropped")

    ing._session = session
    await asyncio.wait_for(ing.run(), 10)
    return waits[:attempts]  # the loop also picks a wait after the final, clean session end


async def test_flapping_sessions_keep_backing_off(plant):
    waits = await _waits(plant, lasting_s=0.001)
    expected = [0.01, 0.02, 0.04, 0.08, 0.08]  # doubling up to the maximum, plus up to 20 % jitter
    assert all(b <= w <= b * 1.2 + 1e-9 for w, b in zip(waits, expected, strict=True)), waits


async def test_a_session_that_lasted_resets_the_back_off(plant):
    waits = await _waits(plant, lasting_s=MAX_S)
    assert all(MIN_S <= w <= MIN_S * 1.2 + 1e-9 for w in waits), waits
