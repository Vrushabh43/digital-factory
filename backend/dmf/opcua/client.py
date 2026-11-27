"""Strictly read-only OPC UA access to the factory.

``ReadOnlyClient`` is the ONLY place in the backend that talks to the OPC UA server.
It deliberately exposes connect / read / subscribe / browse-name checks and nothing
else: no write, no method call, no node management. ``tests/unit/test_readonly_guard.py``
scans the whole ``dmf`` package to make sure no write API is used anywhere, and the
integration tests run against a mock server that counts client write attempts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from asyncua import Client, Node, ua

log = logging.getLogger(__name__)

# How many nodes are sent per Read / CreateMonitoredItems request.
CHUNK = 100


@dataclass(frozen=True)
class Reading:
    value: Any
    good: bool
    status: str
    source_ts: float | None  # epoch seconds, PLC/server clock (may be unsynchronised)


def to_reading(dv: ua.DataValue) -> Reading:
    status = dv.StatusCode
    good = status is None or status.is_good()
    name = "Good" if status is None else status.name
    value = dv.Value.Value if dv.Value is not None else None
    ts = dv.SourceTimestamp or dv.ServerTimestamp
    return Reading(value, good and value is not None, name, ts.timestamp() if ts else None)


class _Handler:
    """asyncua subscription handler -> plain callbacks."""

    def __init__(self, on_change: Callable[[Node, ua.DataValue], None], on_status: Callable[[ua.StatusCode], None]):
        self._on_change = on_change
        self._on_status = on_status

    def datachange_notification(self, node: Node, val: Any, data: Any) -> None:  # asyncua API
        self._on_change(node, data.monitored_item.Value)

    def status_change_notification(self, status: Any) -> None:  # asyncua API
        code = getattr(status, "Status", status)
        self._on_status(code)


class ReadOnlyClient:
    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float = 4.0,
        username: str | None = None,
        password: str | None = None,
        security: str | None = None,
        watchdog_s: float = 1.0,
    ) -> None:
        self.endpoint = endpoint
        self._client = Client(endpoint, timeout=timeout_s, watchdog_intervall=watchdog_s)
        self._client.name = self._client.description = "DMF Tracking Dashboard (read-only)"
        self._client.application_uri = "urn:dmf:tracking-dashboard"
        if username:
            self._client.set_user(username)
        if password:
            self._client.set_password(password)
        self._security = security
        self._subscription: Any = None
        self._open = False
        self.namespace_index: int | None = None

    # --------------------------------------------------------------- session
    async def connect(self) -> None:
        if self._security:
            # format: "Policy,Mode,client_cert.pem,client_key.pem[,server_cert.der]"
            await self._client.set_security_string(self._security)
        self._open = True
        await self._client.connect()

    async def disconnect(self) -> None:
        if not self._open:
            return
        self._open = False
        try:
            if self._subscription is not None:
                await self._subscription.delete()
        except Exception as exc:  # noqa: BLE001 - connection may already be gone
            log.debug("subscription cleanup failed: %s", exc)
        self._subscription = None
        try:
            await self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            log.debug("disconnect failed: %s", exc)

    async def check_connection(self) -> None:
        """Raises if the watchdog, channel renewal or publish loop has failed."""
        await self._client.check_connection()

    async def resolve_namespace(self, uri: str) -> int:
        self.namespace_index = await self._client.get_namespace_index(uri)
        return self.namespace_index

    def node(self, numeric_id: int) -> Node:
        if self.namespace_index is None:
            raise RuntimeError("namespace not resolved")
        return self._client.get_node(ua.NodeId(numeric_id, self.namespace_index))

    # --------------------------------------------------------------- reads
    async def read(self, nodes: Sequence[Node]) -> list[Reading]:
        out: list[Reading] = []
        for i in range(0, len(nodes), CHUNK):
            dvs = await self._client.read_attributes(list(nodes[i : i + CHUNK]), ua.AttributeIds.Value)
            out.extend(to_reading(dv) for dv in dvs)
        return out

    async def read_browse_names(self, nodes: Sequence[Node]) -> list[tuple[str | None, str]]:
        """(browse name or None, status name) for each node."""
        out: list[tuple[str | None, str]] = []
        for i in range(0, len(nodes), CHUNK):
            dvs = await self._client.read_attributes(list(nodes[i : i + CHUNK]), ua.AttributeIds.BrowseName)
            for dv in dvs:
                ok = dv.StatusCode is None or dv.StatusCode.is_good()
                qn = dv.Value.Value if ok and dv.Value is not None else None
                out.append((qn.Name if qn is not None else None, "Good" if ok else dv.StatusCode.name))
        return out

    async def read_access(self, nodes: Sequence[Node]) -> list[int | None]:
        """UserAccessLevel bits (1 = read, 2 = write) — used only to REPORT whether our user could write."""
        out: list[int | None] = []
        for i in range(0, len(nodes), CHUNK):
            dvs = await self._client.read_attributes(list(nodes[i : i + CHUNK]), ua.AttributeIds.UserAccessLevel)
            out.extend(dv.Value.Value if dv.Value is not None and dv.StatusCode.is_good() else None for dv in dvs)
        return out

    # --------------------------------------------------------------- subscription
    async def subscribe(
        self,
        nodes: Iterable[Node],
        on_change: Callable[[Node, ua.DataValue], None],
        on_status: Callable[[ua.StatusCode], None],
        *,
        publishing_ms: int,
        sampling_ms: int,
        queue_size: int,
    ) -> list[tuple[Node, str]]:
        """Monitor ``nodes``; returns (node, status name) for every item the server refused."""
        params = ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=publishing_ms,
            RequestedLifetimeCount=600,
            RequestedMaxKeepAliveCount=20,
            MaxNotificationsPerPublish=0,
            PublishingEnabled=True,
            Priority=0,
        )
        self._subscription = await self._client.create_subscription(params, _Handler(on_change, on_status))
        refused: list[tuple[Node, str]] = []
        node_list = list(nodes)
        for i in range(0, len(node_list), CHUNK):
            chunk = node_list[i : i + CHUNK]
            results = await self._subscription.subscribe_data_change(chunk, queuesize=queue_size, sampling_interval=sampling_ms)
            for node, res in zip(chunk, results, strict=True):
                if isinstance(res, ua.StatusCode):
                    refused.append((node, res.name))
        return refused
