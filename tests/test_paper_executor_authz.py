import unittest
from types import SimpleNamespace

from trading_ai.execution.paper_executor_authz import (
    CAPABILITY_CANCEL,
    CAPABILITY_HEALTH,
    CAPABILITY_KILL,
    CAPABILITY_OBSERVE,
    CAPABILITY_OPEN,
    CAPABILITY_REDUCE,
    ExecutorAuthorizationDenied,
    ExecutorAuthorizationPolicyError,
    load_executor_authorization_policy_bytes,
)

MONITOR_UID = 1201
SAFETY_UID = 1202
DAEMON_UID = 1200
IPC_GID = 1300


def policy_bytes(*, extra: str = "") -> bytes:
    return (
        "schema_version: 1\n"
        "socket_group: trading-ai-paper-ipc\n"
        "principals:\n"
        "  - name: trading-ai-monitor\n"
        f"    uid: {MONITOR_UID}\n"
        "    capabilities: [health, observe]\n"
        "  - name: trading-ai-safety\n"
        f"    uid: {SAFETY_UID}\n"
        "    capabilities: [health, observe, reduce, cancel, kill]\n"
        f"{extra}"
    ).encode()


def user_lookup(name: str) -> SimpleNamespace:
    records = {
        "trading-ai-monitor": SimpleNamespace(
            pw_name="trading-ai-monitor",
            pw_uid=MONITOR_UID,
            pw_gid=MONITOR_UID,
        ),
        "trading-ai-safety": SimpleNamespace(
            pw_name="trading-ai-safety",
            pw_uid=SAFETY_UID,
            pw_gid=SAFETY_UID,
        ),
    }
    if name not in records:
        raise KeyError(name)
    return records[name]


def group_lookup(name: str) -> SimpleNamespace:
    if name != "trading-ai-paper-ipc":
        raise KeyError(name)
    return SimpleNamespace(gr_gid=IPC_GID)


def group_list(_name: str, primary_gid: int) -> tuple[int, ...]:
    return (primary_gid, IPC_GID)


def load(payload: bytes | None = None, *, allow_open: bool = False):
    return load_executor_authorization_policy_bytes(
        payload or policy_bytes(),
        daemon_uid=DAEMON_UID,
        allow_open=allow_open,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        group_list=group_list,
    )


class ExecutorAuthorizationPolicyTests(unittest.TestCase):
    def test_loads_static_principals_and_enforces_capabilities(self) -> None:
        policy = load()
        monitor = SimpleNamespace(uid=MONITOR_UID)
        safety = SimpleNamespace(uid=SAFETY_UID)

        self.assertTrue(policy.admit(monitor))
        self.assertEqual(policy.require(monitor, CAPABILITY_HEALTH).name, "trading-ai-monitor")
        self.assertEqual(policy.require(monitor, CAPABILITY_OBSERVE).uid, MONITOR_UID)
        for capability in (CAPABILITY_REDUCE, CAPABILITY_CANCEL, CAPABILITY_KILL):
            with self.subTest(capability=capability), self.assertRaises(
                ExecutorAuthorizationDenied
            ):
                policy.require(monitor, capability)
        self.assertEqual(policy.require(safety, CAPABILITY_REDUCE).uid, SAFETY_UID)
        self.assertFalse(policy.admit(SimpleNamespace(uid=1999)))
        with self.assertRaises(ExecutorAuthorizationDenied):
            policy.require(SimpleNamespace(uid=1999), CAPABILITY_HEALTH)

    def test_policy_hash_covers_exact_bytes(self) -> None:
        first = load(policy_bytes())
        second = load(policy_bytes(extra="\n"))

        self.assertNotEqual(first.policy_sha256, second.policy_sha256)
        self.assertEqual(len(first.policy_sha256), 64)

    def test_open_capability_requires_explicit_server_side_authority(self) -> None:
        payload = policy_bytes().replace(
            b"[health, observe]",
            b"[health, observe, open]",
            1,
        )
        with self.assertRaisesRegex(
            ExecutorAuthorizationPolicyError,
            "server-side risk authority",
        ):
            load(payload)

        self.assertEqual(
            load(payload, allow_open=True).require(
                SimpleNamespace(uid=MONITOR_UID),
                CAPABILITY_OPEN,
            ).uid,
            MONITOR_UID,
        )

    def test_rejects_unknown_duplicate_or_missing_health_capabilities(self) -> None:
        cases = {
            "unknown": b"[health, observe, admin]",
            "duplicate": b"[health, observe, observe]",
            "missing_health": b"[observe]",
        }
        for label, capabilities in cases.items():
            with self.subTest(label=label), self.assertRaises(
                ExecutorAuthorizationPolicyError
            ):
                load(policy_bytes().replace(b"[health, observe]", capabilities, 1))

    def test_rejects_uid_name_mismatch_daemon_root_and_duplicates(self) -> None:
        cases = {
            "name_mismatch": policy_bytes().replace(
                f"uid: {MONITOR_UID}".encode(),
                b"uid: 1999",
                1,
            ),
            "daemon_uid": policy_bytes().replace(
                f"uid: {MONITOR_UID}".encode(),
                f"uid: {DAEMON_UID}".encode(),
                1,
            ),
            "root_uid": policy_bytes().replace(
                f"uid: {MONITOR_UID}".encode(),
                b"uid: 0",
                1,
            ),
            "duplicate_uid": policy_bytes().replace(
                f"uid: {SAFETY_UID}".encode(),
                f"uid: {MONITOR_UID}".encode(),
                1,
            ),
        }
        for label, payload in cases.items():
            with self.subTest(label=label), self.assertRaises(
                ExecutorAuthorizationPolicyError
            ):
                load(payload)

    def test_rejects_principal_outside_ipc_group(self) -> None:
        with self.assertRaisesRegex(
            ExecutorAuthorizationPolicyError,
            "not a member",
        ):
            load_executor_authorization_policy_bytes(
                policy_bytes(),
                daemon_uid=DAEMON_UID,
                user_lookup=user_lookup,
                group_lookup=group_lookup,
                group_list=lambda _name, primary_gid: (primary_gid,),
            )

    def test_rejects_extra_fields_invalid_yaml_and_unresolved_group(self) -> None:
        invalid_payloads = (
            policy_bytes(extra="unexpected: true\n"),
            b"schema_version: [\n",
            policy_bytes().replace(b"trading-ai-paper-ipc", b"missing-group"),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(
                ExecutorAuthorizationPolicyError
            ):
                load(payload)


if __name__ == "__main__":
    unittest.main()
