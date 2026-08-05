"""Static, default-deny authorization for the paper executor IPC boundary.

The policy binds one systemd service account UID to a small semantic capability
set.  Unix socket group membership is only the first DAC gate; it never grants
an executor operation by itself.  The policy is loaded once from exact bytes by
the credential-owning daemon and is intentionally paper-only.
"""

from __future__ import annotations

import grp
import hashlib
import os
import pwd
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from trading_ai.config import ConfigError, load_yaml_bytes

AUTHZ_SCHEMA_VERSION = 1
EXECUTOR_IPC_GROUP = "trading-ai-paper-ipc"
CAPABILITY_HEALTH = "health"
CAPABILITY_OBSERVE = "observe"
CAPABILITY_REDUCE = "reduce"
CAPABILITY_CANCEL = "cancel"
CAPABILITY_KILL = "kill"
CAPABILITY_OPEN = "open"
EXECUTOR_CAPABILITIES = frozenset(
    {
        CAPABILITY_HEALTH,
        CAPABILITY_OBSERVE,
        CAPABILITY_REDUCE,
        CAPABILITY_CANCEL,
        CAPABILITY_KILL,
        CAPABILITY_OPEN,
    }
)
_NAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,63}\Z")


class ExecutorAuthorizationError(RuntimeError):
    """Base error for an invalid policy or denied executor principal."""


class ExecutorAuthorizationPolicyError(ExecutorAuthorizationError):
    """Raised when a static executor authorization policy is invalid."""


class ExecutorAuthorizationDenied(ExecutorAuthorizationError):
    """Raised when a known or unknown peer lacks a required capability."""


class PeerIdentity(Protocol):
    uid: int


@dataclass(frozen=True)
class ExecutorPrincipal:
    name: str
    uid: int
    capabilities: frozenset[str]


@dataclass(frozen=True)
class ExecutorAuthorizationPolicy:
    """Immutable UID-to-capability mapping loaded at daemon startup."""

    socket_group: str
    socket_gid: int
    policy_sha256: str
    principals: tuple[ExecutorPrincipal, ...]
    _by_uid: Mapping[int, ExecutorPrincipal]

    def admit(self, peer: PeerIdentity) -> bool:
        return type(peer.uid) is int and peer.uid in self._by_uid

    def require(self, peer: PeerIdentity, capability: str) -> ExecutorPrincipal:
        if capability not in EXECUTOR_CAPABILITIES:
            raise ExecutorAuthorizationPolicyError("executor capability is unknown")
        principal = self._by_uid.get(peer.uid) if type(peer.uid) is int else None
        if principal is None or capability not in principal.capabilities:
            raise ExecutorAuthorizationDenied("executor peer is not authorized for this operation")
        return principal


UserLookup = Callable[[str], Any]
GroupLookup = Callable[[str], Any]
GroupList = Callable[[str, int], Sequence[int]]


def load_executor_authorization_policy_bytes(
    payload: bytes,
    *,
    daemon_uid: int | None = None,
    allow_open: bool = False,
    user_lookup: UserLookup = pwd.getpwnam,
    group_lookup: GroupLookup = grp.getgrnam,
    group_list: GroupList = os.getgrouplist,
) -> ExecutorAuthorizationPolicy:
    """Parse exact policy bytes and bind every principal name to its NSS UID."""

    if type(payload) is not bytes or not payload:
        raise ExecutorAuthorizationPolicyError("executor authorization policy bytes are invalid")
    resolved_daemon_uid = os.getuid() if daemon_uid is None else daemon_uid
    if type(resolved_daemon_uid) is not int or resolved_daemon_uid < 1:
        raise ExecutorAuthorizationPolicyError("executor daemon uid is invalid")
    try:
        document = load_yaml_bytes(payload)
    except ConfigError as exc:
        raise ExecutorAuthorizationPolicyError("executor authorization policy YAML is invalid") from exc
    _require_exact_fields(
        document,
        frozenset({"schema_version", "socket_group", "principals"}),
        label="policy",
    )
    if document["schema_version"] != AUTHZ_SCHEMA_VERSION:
        raise ExecutorAuthorizationPolicyError("executor authorization policy schema is unsupported")
    socket_group = _name(document["socket_group"], label="socket_group")
    if socket_group != EXECUTOR_IPC_GROUP:
        raise ExecutorAuthorizationPolicyError("executor IPC group name is not the fixed service group")
    try:
        group_record = group_lookup(socket_group)
        socket_gid = group_record.gr_gid
    except (KeyError, TypeError, AttributeError) as exc:
        raise ExecutorAuthorizationPolicyError("executor IPC group cannot be resolved") from exc
    if type(socket_gid) is not int or socket_gid < 1:
        raise ExecutorAuthorizationPolicyError("executor IPC group id is invalid")

    raw_principals = document["principals"]
    if not isinstance(raw_principals, list) or not raw_principals:
        raise ExecutorAuthorizationPolicyError("executor authorization principals must be a non-empty list")
    principals: list[ExecutorPrincipal] = []
    seen_names: set[str] = set()
    seen_uids: set[int] = set()
    for raw in raw_principals:
        if not isinstance(raw, dict):
            raise ExecutorAuthorizationPolicyError("executor authorization principal must be an object")
        _require_exact_fields(
            raw,
            frozenset({"name", "uid", "capabilities"}),
            label="principal",
        )
        name = _name(raw["name"], label="principal name")
        uid = raw["uid"]
        if type(uid) is not int or uid < 1 or uid == resolved_daemon_uid:
            raise ExecutorAuthorizationPolicyError("executor authorization principal uid is invalid")
        if name in seen_names or uid in seen_uids:
            raise ExecutorAuthorizationPolicyError("executor authorization principal is duplicated")
        try:
            user_record = user_lookup(name)
            resolved_uid = user_record.pw_uid
            primary_gid = user_record.pw_gid
            resolved_name = user_record.pw_name
        except (KeyError, TypeError, AttributeError) as exc:
            raise ExecutorAuthorizationPolicyError("executor authorization principal cannot be resolved") from exc
        if resolved_name != name or resolved_uid != uid:
            raise ExecutorAuthorizationPolicyError("executor authorization name and uid do not match NSS")
        try:
            supplementary_groups = tuple(group_list(name, primary_gid))
        except (OSError, TypeError, ValueError) as exc:
            raise ExecutorAuthorizationPolicyError("executor principal groups cannot be resolved") from exc
        if socket_gid not in supplementary_groups:
            raise ExecutorAuthorizationPolicyError("executor principal is not a member of the IPC group")
        capabilities = _capabilities(raw["capabilities"], allow_open=allow_open)
        principals.append(
            ExecutorPrincipal(
                name=name,
                uid=uid,
                capabilities=capabilities,
            )
        )
        seen_names.add(name)
        seen_uids.add(uid)

    digest = hashlib.sha256(b"paper-executor-authz-v1\0" + payload).hexdigest()
    ordered = tuple(sorted(principals, key=lambda item: item.uid))
    return ExecutorAuthorizationPolicy(
        socket_group=socket_group,
        socket_gid=socket_gid,
        policy_sha256=digest,
        principals=ordered,
        _by_uid=MappingProxyType({principal.uid: principal for principal in ordered}),
    )


def _capabilities(value: object, *, allow_open: bool) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise ExecutorAuthorizationPolicyError("executor principal capabilities must be a non-empty list")
    if any(type(item) is not str for item in value):
        raise ExecutorAuthorizationPolicyError("executor principal capability is invalid")
    capabilities = tuple(value)
    if len(set(capabilities)) != len(capabilities) or not set(capabilities) <= EXECUTOR_CAPABILITIES:
        raise ExecutorAuthorizationPolicyError("executor principal capability is duplicated or unknown")
    if CAPABILITY_HEALTH not in capabilities:
        raise ExecutorAuthorizationPolicyError("executor principal requires the health capability")
    if CAPABILITY_OPEN in capabilities and not allow_open:
        raise ExecutorAuthorizationPolicyError("executor open capability requires server-side risk authority")
    return frozenset(capabilities)


def _name(value: object, *, label: str) -> str:
    if type(value) is not str or _NAME_RE.fullmatch(value) is None:
        raise ExecutorAuthorizationPolicyError(f"executor authorization {label} is invalid")
    return value


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    label: str,
) -> None:
    if set(value) != expected:
        raise ExecutorAuthorizationPolicyError(f"executor authorization {label} fields are invalid")
