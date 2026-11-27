"""Mock OPC UA server with the same namespace URI, numeric node ids and browse names as
the node map in ``config/opcua_nodes.yaml``.

TEST / DEMO TOOLING ONLY. It lets the backend, the integration tests and the end-to-end
tests run without the physical factory. It does not prove anything about the real plant
server; see docs/08-lab-validation.md for the checks to run in the laboratory.

Read-only for clients: every request of a service that could change the server -- Write,
Call, AddNodes, DeleteNodes, AddReferences, DeleteReferences, HistoryUpdate -- is refused
(``BadUserAccessDenied``, as for a read-only user) and counted per service in
``client_attempts`` (``client_write_attempts`` = their sum), so tests can prove the backend
never writes. The variables additionally have no write access bits.

Server-side controls that imitate situations of the real plant (all used by tests and by
the control API in ``dmf_mock.control``):

* ``set(key, value, good=...)``     -- the PLC pushes a new value (or Bad quality)
* ``set_position_quality(pos, good)`` -- every node of one position Bad / Good again
  (e.g. the PLC of that cell lost its connection to the OPC UA server)
* ``freeze_timestamps(pos, age_s)`` -- SourceTimestamp of the position's nodes stays at
  ``now - age_s`` while values may still change: a server that keeps the last value of a
  switched-off PLC (for the timestamp based offline detection)
* ``advance_timestamps(pos, seconds)`` -- move the frozen timestamp forward by ``seconds``;
  ``None`` = refresh to "now" and stop freezing
* ``refuse_monitoring(keys, status)`` -- the server refuses to create monitored items for these
  nodes (e.g. ``BadTooManyMonitoredItems``); applies to subscriptions created afterwards
* ``send_subscription_status(status)`` -- a StatusChangeNotification (e.g. ``BadTimeout``) on
  every subscription, as a server sends when it dropped a subscription
"""

from __future__ import annotations

import copy
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from asyncua import Server, ua
from asyncua.common.callback import CallbackType
from asyncua.crypto.permission_rules import PermissionRuleset

from dmf.plant.model import K_ALARM, K_ENABLE, Plant

log = logging.getLogger(__name__)

# services a read-only client must never use (request type -> service name)
MODIFYING_SERVICES = {
    ua.NodeId(ua.ObjectIds.WriteRequest_Encoding_DefaultBinary): "Write",
    ua.NodeId(ua.ObjectIds.CallRequest_Encoding_DefaultBinary): "Call",
    ua.NodeId(ua.ObjectIds.AddNodesRequest_Encoding_DefaultBinary): "AddNodes",
    ua.NodeId(ua.ObjectIds.DeleteNodesRequest_Encoding_DefaultBinary): "DeleteNodes",
    ua.NodeId(ua.ObjectIds.AddReferencesRequest_Encoding_DefaultBinary): "AddReferences",
    ua.NodeId(ua.ObjectIds.DeleteReferencesRequest_Encoding_DefaultBinary): "DeleteReferences",
    ua.NodeId(ua.ObjectIds.HistoryUpdateRequest_Encoding_DefaultBinary): "HistoryUpdate",
}
_POS_KEY = re.compile(r"^pos(\d+)\.")


def status_code(name: str) -> ua.StatusCode:
    """``"BadTimeout"`` -> StatusCode (ValueError for unknown names)."""
    value = getattr(ua.StatusCodes, name, None) if name.isidentifier() else None
    if not isinstance(value, int):
        raise ValueError(f"unknown OPC UA status code {name!r}")
    return ua.StatusCode(value)


class _ReadOnlyClients(PermissionRuleset):
    """Refuses (and reports) every modifying service; everything else is allowed."""

    def __init__(self, on_attempt) -> None:
        self._on_attempt = on_attempt

    def check_validity(self, user, action_type_id, body) -> bool:
        service = MODIFYING_SERVICES.get(action_type_id)
        if service is None:
            return True
        self._on_attempt(service)
        return False


class MockPlantServer:
    def __init__(self, plant: Plant, endpoint: str = "opc.tcp://0.0.0.0:4840/", *, extra_nodes: bool = True):
        self.plant = plant
        self.endpoint = endpoint
        self.server = Server()
        self.ns: int | None = None
        self.key_to_nodeid: dict[str, ua.NodeId] = {}
        self.values: dict[str, Any] = {}  # last GOOD value per key
        self.bad: set[str] = set()  # keys currently published with Bad quality
        self.client_attempts: Counter[str] = Counter()  # refused modifying requests per service
        self._frozen_ts: dict[int, datetime] = {}  # position -> SourceTimestamp used for its nodes
        self._refused: dict[ua.NodeId, ua.StatusCode] = {}
        self._extra_nodes = extra_nodes
        self._running = False

    @property
    def client_write_attempts(self) -> int:
        return sum(self.client_attempts.values())

    async def start(self) -> None:
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("DMF mock plant server (TEST ONLY)")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity], permission_ruleset=_ReadOnlyClients(self._attempt))
        self.ns = await self.server.register_namespace(self.plant.namespace_uri)
        await self._build()
        self.server.subscribe_server_callback(CallbackType.PreWrite, self._on_pre_write)
        subs = self.server.iserver.subscription_service
        create_items = subs.create_monitored_items

        async def create_monitored_items(params: ua.CreateMonitoredItemsParameters) -> list[ua.MonitoredItemCreateResult]:
            return await self._create_monitored_items(create_items, params)

        subs.create_monitored_items = create_monitored_items  # type: ignore[method-assign]
        await self.server.start()
        self._running = True
        log.info("mock OPC UA server listening on %s (namespace %s = %s)", self.endpoint, self.ns, self.plant.namespace_uri)

    async def stop(self) -> None:
        if self._running:
            self._running = False
            await self.server.stop()

    async def _build(self) -> None:
        assert self.ns is not None
        objects = self.server.nodes.objects
        root = await objects.add_folder(ua.NodeId("DMF", self.ns), ua.QualifiedName("DMF", self.ns))
        general = await root.add_folder(ua.NodeId("General", self.ns), ua.QualifiedName("General", self.ns))
        await self._var(general, K_ALARM, self.plant.general_nodes["alarm"], "Alarm", False)
        await self._var(general, K_ENABLE, self.plant.general_nodes["enable"], "Freischalten", True)
        signals = {s.key: s for s in self.plant.signal_nodes(include_sensors=True)}
        for pos, _cell in sorted(self.plant.cells.items()):
            folder = await root.add_folder(ua.NodeId(f"Zelle{pos}", self.ns), ua.QualifiedName(f"Zelle{pos}", self.ns))
            for key, sig in signals.items():
                if not key.startswith(f"pos{pos}."):
                    continue
                if key.endswith(".lamp"):
                    await self._var(folder, key, sig.node_id, sig.browse_name, 3, ua.VariantType.Int16)
                elif key.endswith(".state"):
                    await self._var(folder, key, sig.node_id, sig.browse_name, 0, ua.VariantType.Int16)
                else:
                    await self._var(folder, key, sig.node_id, sig.browse_name, False)

    async def _var(self, parent, key: str, numeric_id: int, name: str, value: Any, vtype: ua.VariantType | None = None):
        nodeid = ua.NodeId(numeric_id, self.ns)
        vtype = vtype or ua.VariantType.Boolean
        node = await parent.add_variable(nodeid, ua.QualifiedName(name, self.ns), ua.Variant(value, vtype))
        # read-only for clients (no write access bits)
        await node.set_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.CurrentRead)
        await node.unset_attr_bit(ua.AttributeIds.AccessLevel, ua.AccessLevel.CurrentWrite)
        self.key_to_nodeid[key] = nodeid
        self.values[key] = value
        return node

    def _attempt(self, service: str) -> None:
        self.client_attempts[service] += 1
        log.error("client %s request refused by the read-only mock server", service)

    async def _on_pre_write(self, event: Any, dispatcher: Any) -> None:
        # not reached for client writes (refused before); kept as a second line of defence
        if getattr(event, "is_external", True):
            self._attempt("Write")
            log.error("client write attempt on mock server: %s", event.request_params)

    async def _create_monitored_items(
        self, create, params: ua.CreateMonitoredItemsParameters
    ) -> list[ua.MonitoredItemCreateResult]:
        refused = {
            i: self._refused[it.ItemToMonitor.NodeId]
            for i, it in enumerate(params.ItemsToCreate)
            if it.ItemToMonitor.NodeId in self._refused
        }
        if not refused:
            return await create(params)
        keep = [it for i, it in enumerate(params.ItemsToCreate) if i not in refused]
        rest = copy.copy(params)
        rest.ItemsToCreate = keep
        created = iter(await create(rest) if keep else [])
        out = []
        for i in range(len(params.ItemsToCreate)):
            if i in refused:
                res = ua.MonitoredItemCreateResult()
                res.StatusCode = refused[i]
                out.append(res)
            else:
                out.append(next(created))
        return out

    # ------------------------------------------------------------------ server-side updates
    def position_keys(self, pos: int) -> list[str]:
        if pos not in self.plant.cells:
            raise KeyError(f"unknown position {pos}")
        return [k for k in self.key_to_nodeid if k.startswith(f"pos{pos}.")]

    def _source_ts(self, key: str) -> datetime:
        m = _POS_KEY.match(key)
        frozen = self._frozen_ts.get(int(m.group(1))) if m else None
        return frozen or datetime.now(UTC)

    async def set(self, key: str, value: Any, *, good: bool = True) -> None:
        """Change a value server-side (simulates the PLC pushing a new value)."""
        nodeid = self.key_to_nodeid[key]
        vtype = ua.VariantType.Int16 if key.endswith((".lamp", ".state")) else ua.VariantType.Boolean
        if good:
            variant = ua.Variant(int(value) if vtype == ua.VariantType.Int16 else bool(value), vtype)
            dv = ua.DataValue(variant, ua.StatusCode(ua.StatusCodes.Good), SourceTimestamp=self._source_ts(key))
            self.values[key] = value
            self.bad.discard(key)
        else:
            dv = ua.DataValue(
                ua.Variant(None, ua.VariantType.Null),
                ua.StatusCode(ua.StatusCodes.BadCommunicationError),
                SourceTimestamp=self._source_ts(key),
            )
            self.bad.add(key)
        await self.server.iserver.write_attribute_value(nodeid, dv)

    async def set_many(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            if self.values.get(k) != v:
                await self.set(k, v)

    async def set_position_quality(self, pos: int, good: bool) -> int:
        """All nodes of one position Bad (``good=False``) or back to their last good value."""
        keys = self.position_keys(pos)
        for k in keys:
            await self.set(k, self.values[k], good=good)
        return len(keys)

    async def _republish(self, pos: int) -> None:
        for k in self.position_keys(pos):
            await self.set(k, self.values[k], good=k not in self.bad)

    async def freeze_timestamps(self, pos: int, age_s: float = 0.0) -> datetime:
        """From now on the position's nodes carry SourceTimestamp ``now - age_s`` (also on value changes)."""
        self.position_keys(pos)
        ts = datetime.now(UTC) - timedelta(seconds=age_s)
        self._frozen_ts[pos] = ts
        await self._republish(pos)
        return ts

    async def advance_timestamps(self, pos: int, seconds: float | None = None) -> datetime:
        """Move a frozen SourceTimestamp forward by ``seconds``; ``None``: refresh to now and unfreeze."""
        self.position_keys(pos)
        if seconds is None:
            self._frozen_ts.pop(pos, None)
            ts = datetime.now(UTC)
        else:
            ts = self._frozen_ts.get(pos, datetime.now(UTC)) + timedelta(seconds=seconds)
            self._frozen_ts[pos] = ts
        await self._republish(pos)
        return ts

    def frozen_timestamp(self, pos: int) -> datetime | None:
        return self._frozen_ts.get(pos)

    def refuse_monitoring(self, keys: list[str], status: str = "BadTooManyMonitoredItems") -> None:
        """Refuse monitored items for ``keys`` in subscriptions created from now on (empty list: refuse none)."""
        code = status_code(status)
        unknown = [k for k in keys if k not in self.key_to_nodeid]
        if unknown:
            raise KeyError(f"unknown keys: {unknown}")
        self._refused = {self.key_to_nodeid[k]: code for k in keys}

    async def send_subscription_status(self, status: str = "BadTimeout") -> int:
        """Send a StatusChangeNotification on every client subscription; returns their number."""
        code = status_code(status)
        subs = list(self.server.iserver.subscription_service.subscriptions.values())
        for sub in subs:
            await sub.enqueue_statuschange(code)
            # asyncua publishes a queued status change only together with data changes or a
            # keep-alive: let the next publishing cycle send it right away
            sub._keep_alive_count = sub.data.RevisedMaxKeepAliveCount + 1
        return len(subs)
