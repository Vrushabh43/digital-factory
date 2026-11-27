"""Static guard: the backend package must never use an OPC UA write / call / node-management API.

The mock server (package ``dmf_mock``) is test tooling and is deliberately excluded.
"""

import ast
import re
from pathlib import Path

DMF = Path(__file__).resolve().parents[2] / "dmf"

FORBIDDEN_ATTRS = {
    "write_value",
    "write_values",
    "write_attribute",
    "write_attributes",
    "write_attribute_value",
    "set_value",
    "set_data_value",
    "write_params",
    "write_array_dimensions",
    "write_data_type",
    "write_value_rank",
    "set_writable",
    "set_read_only",
    "call_method",
    "call",
    "add_nodes",
    "delete_nodes",
    "add_references",
    "delete_references",
    "add_variable",
    "add_object",
    "add_folder",
    "add_method",
    "add_property",
    "delete",
    "set_attr_bit",
    "unset_attr_bit",
    "register_nodes",
}
# ``delete`` is allowed only for removing our own subscription
ALLOWED = {("opcua/client.py", "delete")}


def _py_files():
    return [p for p in DMF.rglob("*.py") if "testing" not in p.parts]


def test_no_write_api_used_anywhere_in_dmf():
    offenders = []
    for path in _py_files():
        rel = path.relative_to(DMF).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS and (rel, node.attr) not in ALLOWED:
                offenders.append(f"{rel}:{node.lineno} .{node.attr}")
    assert not offenders, "OPC UA write/modify API used:\n" + "\n".join(offenders)


def test_only_client_module_imports_asyncua_client():
    pattern = re.compile(r"^\s*(from asyncua import .*\bClient\b|import asyncua\b)", re.M)
    users = [p.relative_to(DMF).as_posix() for p in _py_files() if pattern.search(p.read_text(encoding="utf-8"))]
    assert users == ["opcua/client.py"]


def test_readonly_client_exposes_no_write_methods():
    from dmf.opcua.client import ReadOnlyClient

    public = {n for n in dir(ReadOnlyClient) if not n.startswith("_")}
    assert public == {
        "connect",
        "disconnect",
        "check_connection",
        "resolve_namespace",
        "node",
        "read",
        "read_browse_names",
        "read_access",
        "subscribe",
    }


# --------------------------------------------------------------------------- stricter guards
# Every public method of the asyncua client objects that can change something on the server
# (or that the list above could miss). Derived from the installed asyncua, so new library
# methods are covered automatically.
_MUTATING_PREFIX = re.compile(
    r"(write|set_|unset_|add_|delete|call|register|unregister|history_update|import_|load_|create_|copy|instantiate|trigger|modify)"
)
# Client-side configuration and our own subscription: they do not change the factory.
_CLIENT_SIDE = {
    ("opcua/client.py", "set_user"),
    ("opcua/client.py", "set_password"),
    ("opcua/client.py", "set_security_string"),
    ("opcua/client.py", "create_subscription"),
    ("opcua/client.py", "delete"),  # Subscription.delete: removes only our own subscription
}
# Names that are also everyday Python methods (file.write, ...): only forbidden where asyncua is imported.
_GENERIC = {"write", "delete", "call", "register", "unregister", "copy"}
# OPC UA service request/parameter types of write, method call, node management and history update.
FORBIDDEN_UA_TYPES = {
    "WriteParameters",
    "WriteRequest",
    "WriteValue",
    "CallParameters",
    "CallRequest",
    "CallMethodRequest",
    "AddNodesItem",
    "AddNodesParameters",
    "AddNodesRequest",
    "AddReferencesItem",
    "AddReferencesParameters",
    "AddReferencesRequest",
    "DeleteNodesItem",
    "DeleteNodesParameters",
    "DeleteNodesRequest",
    "DeleteReferencesItem",
    "DeleteReferencesParameters",
    "DeleteReferencesRequest",
    "HistoryUpdateParameters",
    "HistoryUpdateRequest",
    "HistoryUpdateDetails",
    "UpdateDataDetails",
    "UpdateEventDetails",
    "DeleteAtTimeDetails",
    "DeleteEventDetails",
    "DeleteRawModifiedDetails",
    "RegisterNodesParameters",
    "RegisterNodesRequest",
}
_DYNAMIC_ATTR_CALLS = {"getattr", "setattr", "delattr"}
_FORBIDDEN_NAMES = {"eval", "exec", "__import__", "methodcaller", "attrgetter", "__getattribute__", "uaclient"}


def _mutating_names() -> set[str]:
    from asyncua import Client, Node
    from asyncua.client.ua_client import UaClient
    from asyncua.common.subscription import Subscription

    return {
        n
        for cls in (Node, Client, UaClient, Subscription)
        for n in dir(cls)
        if not n.startswith("_") and _MUTATING_PREFIX.match(n)
    }


def _imports_asyncua(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "asyncua" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "asyncua":
            return True
    return False


def _offences(rel: str, tree: ast.AST, mutating: set[str]) -> list[str]:
    uses_asyncua = _imports_asyncua(tree)
    forbidden = mutating if uses_asyncua else mutating - _GENERIC

    def bad_name(name: str) -> bool:
        return name in forbidden and (rel, name) not in _CLIENT_SIDE

    out = []
    for node in ast.walk(tree):
        where = f"{rel}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Attribute):
            if bad_name(node.attr) or node.attr in FORBIDDEN_UA_TYPES or node.attr in _FORBIDDEN_NAMES:
                out.append(f"{where} .{node.attr}")
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_UA_TYPES or node.id in _FORBIDDEN_NAMES):
            out.append(f"{where} {node.id}")
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name.split(".")[-1] in FORBIDDEN_UA_TYPES | _FORBIDDEN_NAMES:
                    out.append(f"{where} import {alias.name}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _DYNAMIC_ATTR_CALLS:
            # dynamic attribute access must name the attribute literally (no "write_" + "value")
            name = node.args[1] if len(node.args) > 1 else None
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                out.append(f"{where} {node.func.id}() with a computed attribute name")
            elif bad_name(name.value) or node.func.id != "getattr" and uses_asyncua:
                out.append(f"{where} {node.func.id}(..., {name.value!r})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in FORBIDDEN_UA_TYPES:
            out.append(f"{where} {node.value!r}")
    return out


def test_no_mutating_asyncua_api_and_no_dynamic_attribute_access():
    mutating = _mutating_names()
    # the generated list covers the hand-written one and the gaps it had
    assert {"write_value", "set_values", "register_namespace", "add_reference", "write_data_type_definition"} <= mutating
    offenders = []
    for path in _py_files():
        rel = path.relative_to(DMF).as_posix()
        offenders += _offences(rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), mutating)
    assert not offenders, "OPC UA write/modify API or dynamic attribute access in dmf:\n" + "\n".join(offenders)


def test_the_guard_catches_evasions():
    mutating = _mutating_names()
    evasions = [
        'await getattr(node, "write_" + "value")(False)',
        'await getattr(node, "write_value")(False)',
        'name = "write_value"\nawait getattr(node, name)(1)',
        "await node.write_attribute(13, dv)",
        "await client.set_values([node], [1])",
        "await client.register_namespace('urn:x')",
        "await node.add_reference(other, 40)",
        "await client.uaclient.write(params)",
        "params = ua.WriteParameters()",
        "req = ua.CallMethodRequest()",
        "items = [ua.AddNodesItem()]",
        "from asyncua.ua import HistoryUpdateParameters",
        "await node.__getattribute__('write_value')(1)",
        "f = operator.methodcaller('write_value', 1)",
        "setattr(node, 'x', 1)",
    ]
    for src in evasions:
        tree = ast.parse("from asyncua import ua\nasync def f(node, client, other, params, dv, operator):\n" + _indent(src))
        assert _offences("opcua/ingest.py", tree, mutating), src
    harmless = ['x = getattr(code, "name", str(code))', "await client.read([node])", "fh.write('line')"]
    for src in harmless:
        tree = ast.parse("async def f(node, client, code, fh):\n" + _indent(src))
        assert not _offences("cli/record.py", tree, mutating), src


def _indent(src: str) -> str:
    return "".join("    " + line + "\n" for line in src.splitlines())
