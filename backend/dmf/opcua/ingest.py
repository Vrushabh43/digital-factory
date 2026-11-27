"""OPC UA ingest: connection lifecycle, subscription, reconnect and resynchronisation.

Produces an ordered stream of messages on an ``asyncio.Queue``:

* ``Snapshot`` — complete set of current values after every (re)connect. It is built
  from the subscription's *initial notifications* (so later notifications can never be
  older than the snapshot); values the server did not send in time are read once.
* ``Change`` — one value change, in server order.
* ``Lost`` — connection lost (pieces are kept, cells shown offline).
* ``Stale`` — optional per-cell "no fresh data" detection (OFFLINE_DETECTION=timestamps).

All times passed on are taken from the backend clock; PLC timestamps are only used for
the optional stale check.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from asyncua import Node, ua

from dmf.opcua.client import ReadOnlyClient, to_reading
from dmf.plant.model import Plant, SignalNode
from dmf.settings import Settings, redact_url

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    values: dict[str, tuple[Any, bool]]
    ts: float
    reason: str


@dataclass
class Change:
    key: str
    value: Any
    good: bool
    ts: float


@dataclass
class Lost:
    reason: str
    ts: float


@dataclass
class Stale:
    pos: int
    stale: bool
    ts: float


@dataclass
class Tick:
    """Periodic tick, sent by the tracker service through the same queue (keeps order)."""

    ts: float


Message = Snapshot | Change | Lost | Stale | Tick


@dataclass
class NodeCheck:
    key: str
    node_id: int
    expected: str
    actual: str | None
    status: str

    @property
    def ok(self) -> bool:
        return self.status == "Good" and self.actual == self.expected


@dataclass
class IngestStatus:
    state: str = "starting"  # starting | connecting | connected | disconnected | disabled
    endpoint: str = ""
    namespace_index: int | None = None
    connected_since: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    reconnect_attempts: int = 0
    next_retry_at: float | None = None
    subscribed: int = 0
    refused: list[dict[str, str]] = field(default_factory=list)
    node_mismatches: list[dict[str, Any]] = field(default_factory=list)
    nodes_checked: int = 0
    last_notification_at: float | None = None
    notifications: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class LostConnection(Exception):
    pass


class OpcIngest:
    def __init__(
        self,
        plant: Plant,
        settings: Settings,
        queue: asyncio.Queue[Message],
        *,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[], ReadOnlyClient] | None = None,
    ) -> None:
        self.plant = plant
        self.s = settings
        self.queue = queue
        self.clock = clock
        self.status = IngestStatus(endpoint=redact_url(settings.opcua_endpoint))
        self._client_factory = client_factory or self._default_client
        self.signals: list[SignalNode] = plant.signal_nodes(include_sensors=settings.opcua_subscribe_sensors)
        self._by_nodeid: dict[ua.NodeId, str] = {}
        self._collecting: dict[str, tuple[Any, bool]] | None = None
        self._lost = asyncio.Event()
        self._lost_reason = ""
        self._stop = asyncio.Event()
        self._stale: dict[int, bool] = {}
        self._ts_seen: dict[int, tuple[float | None, float]] = {}  # pos -> (source ts, local time it changed)
        self._untrusted: set[str] = set()  # keys whose node failed the browse-name check
        self._floor: dict[str, float] = {}  # key -> source ts of a value read after subscribing

    def _default_client(self) -> ReadOnlyClient:
        return ReadOnlyClient(
            self.s.opcua_endpoint,
            timeout_s=self.s.opcua_timeout_s,
            username=self.s.opcua_username,
            password=self.s.opcua_password,
            security=self.s.opcua_security,
        )

    # ================================================================ lifecycle
    async def run(self) -> None:
        delay = self.s.opcua_reconnect_min_s
        was_connected = False
        while not self._stop.is_set():
            client = self._client_factory()
            self.status.state = "connecting"
            self.status.next_retry_at = None
            try:
                await self._session(client, "reconnected" if was_connected else "connected")
            except asyncio.CancelledError:
                await client.disconnect()
                raise
            except Exception as exc:  # noqa: BLE001 - any failure leads to a reconnect
                reason = _describe(exc)
                self._note_error(reason)
                log.warning("OPC UA session ended: %s", reason)
            finally:
                await client.disconnect()
            if self.status.state == "connected" or was_connected:
                was_connected = True
            if self.status.connected_since is not None:
                if self.clock() - self.status.connected_since >= self.s.opcua_reconnect_max_s:
                    delay = self.s.opcua_reconnect_min_s  # it worked for a while: retry quickly
                # else: a session that drops right after connecting keeps backing off, so a
                # flapping server is not hammered and the history not flooded with flaps
                self._emit(Lost(self._lost_reason or self.status.last_error or "connection lost", self.clock()))
            self.status.state = "disconnected"
            self.status.connected_since = None
            self.status.reconnect_attempts += 1
            jitter = random.uniform(0, delay * 0.2)
            self.status.next_retry_at = self.clock() + delay + jitter
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay + jitter)
            except TimeoutError:
                pass
            delay = min(delay * 2, self.s.opcua_reconnect_max_s)

    async def stop(self) -> None:
        self._stop.set()
        self._lost_reason = "backend stopping"
        self._lost.set()

    def request_resync(self, reason: str) -> None:
        """End the current session; the reconnect delivers a complete, fresh snapshot."""
        if self.status.state == "connected" and not self._lost.is_set():
            self._lost_reason = reason
            self._lost.set()

    # ================================================================ one session
    async def _session(self, client: ReadOnlyClient, reason: str) -> None:
        self._lost.clear()
        self._lost_reason = ""
        log.info("connecting to %s", redact_url(self.s.opcua_endpoint))
        await client.connect()
        ns = await client.resolve_namespace(self.plant.namespace_uri)
        self.status.namespace_index = ns
        nodes = [client.node(sig.node_id) for sig in self.signals]
        self._by_nodeid = {n.nodeid: sig.key for n, sig in zip(nodes, self.signals, strict=True)}
        self._untrusted = set()
        self._floor = {}
        self._ts_seen = {}
        if self.s.opcua_validate_nodes:
            await self._validate(client, nodes)

        # subscribe while collecting the initial values into a snapshot
        self._collecting = {}
        refused = await client.subscribe(
            nodes,
            self._on_change,
            self._on_status,
            publishing_ms=self.s.opcua_publishing_interval_ms,
            sampling_ms=self.s.opcua_sampling_interval_ms,
            queue_size=self.s.opcua_queue_size,
        )
        self.status.refused = [{"key": self._by_nodeid[n.nodeid], "status": st} for n, st in refused]
        self.status.subscribed = len(nodes) - len(refused)
        for n, st in refused:
            log.warning("server refused monitoring %s (%s): %s", self._by_nodeid[n.nodeid], n.nodeid, st)
        refused_keys = {self._by_nodeid[n.nodeid] for n, _ in refused}
        await self._wait_initial(len(nodes) - len(refused))
        missing = [(n, sig) for n, sig in zip(nodes, self.signals, strict=True) if sig.key not in self._collecting]
        if missing:
            readings = await client.read([n for n, _ in missing])
            for (_n, sig), r in zip(missing, readings, strict=True):
                good = r.good and sig.key not in refused_keys and sig.key not in self._untrusted
                if sig.key not in self._collecting and r.source_ts is not None:
                    # the server's (late) initial notification may carry an OLDER value
                    self._floor[sig.key] = r.source_ts
                self._collecting.setdefault(sig.key, (r.value, good))
        snapshot, self._collecting = self._collecting, None  # atomic switch to forwarding mode
        now = self.clock()
        self.status.state = "connected"
        self.status.connected_since = now
        self.status.last_error = None
        self.status.reconnect_attempts = 0
        self._emit(Snapshot(snapshot, now, reason))
        log.info("connected: %d signals subscribed, %d refused", self.status.subscribed, len(refused))

        stale_task = None
        if self.s.offline_detection == "timestamps":
            stale_task = asyncio.create_task(self._stale_loop(client))
        try:
            while not self._lost.is_set():
                try:
                    await asyncio.wait_for(self._lost.wait(), timeout=1.0)
                except TimeoutError:
                    await client.check_connection()
                if stale_task is not None and stale_task.done():
                    exc = stale_task.exception()
                    raise LostConnection(f"offline check failed: {_describe(exc) if exc else 'stopped'}")
            raise LostConnection(self._lost_reason or "connection lost")
        finally:
            if stale_task is not None:
                stale_task.cancel()

    async def _wait_initial(self, expected: int) -> None:
        deadline = self.clock() + max(2.0, self.s.opcua_timeout_s)
        while self._collecting is not None and len(self._collecting) < expected and self.clock() < deadline:
            await asyncio.sleep(0.05)

    async def _validate(self, client: ReadOnlyClient, nodes: list[Node]) -> None:
        names = await client.read_browse_names(nodes)
        checks = [
            NodeCheck(sig.key, sig.node_id, sig.browse_name, actual, st)
            for sig, (actual, st) in zip(self.signals, names, strict=True)
        ]
        bad = [c for c in checks if not c.ok]
        self.status.nodes_checked = len(checks)
        self.status.node_mismatches = [c.__dict__ | {"ok": False} for c in bad]
        # A node whose browse name does not match is not trusted: its values are treated as
        # Bad quality (the affected cell shows "no valid data") instead of being guessed.
        self._untrusted = {c.key for c in bad}
        if bad:
            log.warning(
                "node map check: %d of %d nodes do not match (first: %s id=%s expected %r got %r %s) — "
                "see /api/status; their values are IGNORED (treated as Bad). Lab verification required.",
                len(bad),
                len(checks),
                bad[0].key,
                bad[0].node_id,
                bad[0].expected,
                bad[0].actual,
                bad[0].status,
            )
        else:
            log.info("node map check: all %d nodes match their expected browse names", len(checks))

    async def _stale_loop(self, client: ReadOnlyClient) -> None:
        """A cell is stale when the SourceTimestamp of its lamp has not ADVANCED for
        ``offline_after_s`` of local time. Only changes of the timestamp are compared,
        never the timestamp with the local clock, so a PLC clock that is off by minutes
        (no NTP in the lab) does not make every cell look offline."""
        lamps = [(pos, client.node(cell.lamp_node)) for pos, cell in sorted(self.plant.cells.items())]
        while True:
            await asyncio.sleep(self.s.offline_check_interval_s)
            readings = await client.read([n for _, n in lamps])
            now = self.clock()
            for (pos, _), r in zip(lamps, readings, strict=True):
                last_ts, changed_at = self._ts_seen.get(pos, (None, now))
                if r.source_ts is not None and r.source_ts != last_ts:
                    changed_at = now
                self._ts_seen[pos] = (r.source_ts, changed_at)
                stale = r.source_ts is None or now - changed_at > self.s.offline_after_s
                if self._stale.get(pos) != stale:
                    self._stale[pos] = stale
                    self._emit(Stale(pos, stale, now))

    # ================================================================ callbacks (event loop thread)
    def _on_change(self, node: Node, dv: ua.DataValue) -> None:
        key = self._by_nodeid.get(node.nodeid)
        if key is None:
            return
        r = to_reading(dv)
        self.status.notifications += 1
        self.status.last_notification_at = self.clock()
        good = r.good and key not in self._untrusted
        if self._collecting is not None:
            self._collecting[key] = (r.value, good)
            return
        floor = self._floor.pop(key, None)
        if floor is not None and r.source_ts is not None and r.source_ts < floor:
            return  # late initial notification, older than the value already read
        self._emit(Change(key, r.value, good, self.clock()))

    def _on_status(self, code: Any) -> None:
        name = getattr(code, "name", str(code))
        log.warning("subscription status change: %s", name)
        self._lost_reason = f"subscription status {name}"
        self._lost.set()

    def _emit(self, msg: Message) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            # the tracker cannot keep up: drop the session and resynchronise from a fresh snapshot
            log.error("event queue full — forcing a resynchronisation")
            self._lost_reason = "event queue overflow"
            self._lost.set()

    def _note_error(self, reason: str) -> None:
        self.status.last_error = reason
        self.status.last_error_at = self.clock()


def _describe(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, ua.UaStatusCodeError):
        text = f"{exc.__class__.__name__}: {text}"
    elif isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        text = f"{exc.__class__.__name__}: {text}"
    return text[:300]
