from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from trading_ai.execution import paper_executor_daemon as daemon
from trading_ai.execution.paper_executor_journal import ExecutorRunState

TEST_API_KEY = "test-paper-api-key"  # noqa: S105 - inert test credential
TEST_SECRET_KEY = "test-paper-secret-key"  # noqa: S105 - inert test credential
TEST_ACCOUNT_ID = "f9ef2f82-c09b-4af0-a439-243fe31f77d9"


class SystemdCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_private(path: Path, payload: bytes, *, mode: int = 0o400) -> None:
        path.write_bytes(payload)
        path.chmod(mode)

    def _credential_directory(self, name: str = "credentials") -> Path:
        directory = self.root / name
        directory.mkdir(mode=0o700)
        self._write_private(directory / "alpaca-paper-api-key", TEST_API_KEY.encode())
        self._write_private(
            directory / "alpaca-paper-secret-key",
            TEST_SECRET_KEY.encode(),
        )
        directory.chmod(0o500)
        return directory

    def test_loads_private_credentials_from_absolute_real_directory(self) -> None:
        directory = self._credential_directory()

        credentials = daemon.load_systemd_credentials({"CREDENTIALS_DIRECTORY": str(directory)})

        self.assertEqual(credentials.api_key, TEST_API_KEY)
        self.assertEqual(credentials.secret_key, TEST_SECRET_KEY)

    def test_credential_directory_is_required_and_must_be_absolute(self) -> None:
        for value in (None, "relative/credentials"):
            with self.subTest(value=value):
                environment = {} if value is None else {"CREDENTIALS_DIRECTORY": value}
                with self.assertRaisesRegex(
                    daemon.PaperExecutorDaemonError,
                    "CREDENTIALS_DIRECTORY is required",
                ):
                    daemon.load_systemd_credentials(environment)

    def test_credential_directory_rejects_symlink_and_non_directory(self) -> None:
        real_directory = self._credential_directory("real")
        linked_directory = self.root / "linked"
        linked_directory.symlink_to(real_directory, target_is_directory=True)
        ordinary_file = self.root / "ordinary-file"
        ordinary_file.write_text("not a directory", encoding="utf-8")

        for path in (linked_directory, ordinary_file):
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    daemon.PaperExecutorDaemonError,
                    "must be a private real directory",
                ),
            ):
                daemon.load_systemd_credentials({"CREDENTIALS_DIRECTORY": str(path)})

    def test_credential_entries_reject_symlink_and_non_regular_file(self) -> None:
        symlink_directory = self._credential_directory("symlink-entry")
        symlink_directory.chmod(0o700)
        api_path = symlink_directory / "alpaca-paper-api-key"
        api_path.unlink()
        target = self.root / "api-target"
        self._write_private(target, TEST_API_KEY.encode())
        api_path.symlink_to(target)
        symlink_directory.chmod(0o500)

        non_regular_directory = self._credential_directory("non-regular-entry")
        non_regular_directory.chmod(0o700)
        non_regular_api = non_regular_directory / "alpaca-paper-api-key"
        non_regular_api.unlink()
        non_regular_api.mkdir(mode=0o700)
        non_regular_directory.chmod(0o500)

        for directory in (symlink_directory, non_regular_directory):
            with self.subTest(directory=directory), self.assertRaises(daemon.PaperExecutorDaemonError):
                daemon.load_systemd_credentials({"CREDENTIALS_DIRECTORY": str(directory)})

    def test_credential_directory_rejects_owner_write_and_broad_permissions(self) -> None:
        for name, mode in (
            ("owner-writable", 0o700),
            ("group-accessible", 0o550),
            ("world-accessible", 0o505),
        ):
            directory = self._credential_directory(name)
            directory.chmod(mode)

            with (
                self.subTest(mode=oct(mode)),
                self.assertRaisesRegex(
                    daemon.PaperExecutorDaemonError,
                    "private real directory",
                ),
            ):
                daemon.load_systemd_credentials({"CREDENTIALS_DIRECTORY": str(directory)})

    def test_credential_entries_reject_owner_write_and_broad_permissions(self) -> None:
        for name, mode in (
            ("owner-writable", 0o600),
            ("group-readable", 0o440),
            ("world-readable", 0o404),
        ):
            directory = self._credential_directory(name)
            (directory / "alpaca-paper-api-key").chmod(mode)

            with (
                self.subTest(mode=oct(mode)),
                self.assertRaisesRegex(
                    daemon.PaperExecutorDaemonError,
                    "permissions are unsafe",
                ),
            ):
                daemon.load_systemd_credentials({"CREDENTIALS_DIRECTORY": str(directory)})

    def test_credential_entries_reject_empty_and_oversized_values(self) -> None:
        for name, payload in (
            ("empty", b""),
            ("oversized", b"x" * (daemon._MAX_CREDENTIAL_BYTES + 1)),
        ):
            directory = self._credential_directory(name)
            directory.chmod(0o700)
            api_path = directory / "alpaca-paper-api-key"
            api_path.chmod(0o600)
            self._write_private(api_path, payload)
            directory.chmod(0o500)

            with self.subTest(name=name), self.assertRaises(daemon.PaperExecutorDaemonError):
                daemon.load_systemd_credentials({"CREDENTIALS_DIRECTORY": str(directory)})


class DaemonPathAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_config(path: Path, payload: bytes = b"config: test\n") -> None:
        path.write_bytes(payload)
        path.chmod(0o400)

    def test_runtime_directory_selects_socket_and_rejects_ambiguous_paths(self) -> None:
        runtime_directory = self.root / "runtime"

        with patch.object(
            daemon,
            "DEFAULT_EXECUTOR_RUNTIME_DIRECTORY",
            runtime_directory,
        ):
            self.assertEqual(
                daemon._runtime_socket_path({"RUNTIME_DIRECTORY": str(runtime_directory)}),
                runtime_directory / "alpaca-paper-executor.sock",
            )
        for raw in (None, "relative/runtime", "/run/one:/run/two"):
            with self.subTest(raw=raw), self.assertRaises(daemon.PaperExecutorDaemonError):
                environment = {} if raw is None else {"RUNTIME_DIRECTORY": raw}
                daemon._runtime_socket_path(environment)
        with self.assertRaisesRegex(daemon.PaperExecutorDaemonError, "fixed executor path"):
            daemon._runtime_socket_path({"RUNTIME_DIRECTORY": str(runtime_directory)})

    def test_state_directory_selects_supervisor_root_and_requires_one_absolute_path(
        self,
    ) -> None:
        state_directory = self.root / "state"

        with patch.object(
            daemon,
            "DEFAULT_EXECUTOR_STATE_DIRECTORY",
            state_directory,
        ):
            self.assertEqual(
                daemon._supervisor_root({"STATE_DIRECTORY": str(state_directory)}),
                state_directory / "account-supervisor",
            )
        for raw in (None, "relative/state", "/state/one:/state/two"):
            with self.subTest(raw=raw):
                environment = {} if raw is None else {"STATE_DIRECTORY": raw}
                with self.assertRaises(daemon.PaperExecutorDaemonError):
                    daemon._supervisor_root(environment)
        with self.assertRaisesRegex(daemon.PaperExecutorDaemonError, "fixed executor path"):
            daemon._supervisor_root({"STATE_DIRECTORY": str(state_directory)})

    def test_service_identity_requires_fixed_nonroot_uid_and_ipc_gid(self) -> None:
        user = SimpleNamespace(
            pw_name=daemon.DEFAULT_EXECUTOR_SERVICE_USER,
            pw_uid=1200,
        )
        group = SimpleNamespace(
            gr_name=daemon.DEFAULT_EXECUTOR_IPC_GROUP,
            gr_gid=1300,
        )
        with (
            patch.object(daemon.os, "getuid", return_value=1200),
            patch.object(daemon.os, "geteuid", return_value=1200),
            patch.object(daemon.os, "getgid", return_value=1300),
            patch.object(daemon.os, "getegid", return_value=1300),
            patch.object(daemon.pwd, "getpwnam", return_value=user),
            patch.object(daemon.grp, "getgrnam", return_value=group),
        ):
            daemon._validate_service_identity()

        invalid_ids = (
            (0, 0, 1300, 1300),
            (1200, 1201, 1300, 1300),
            (1200, 1200, 0, 0),
            (1200, 1200, 1300, 1301),
        )
        for real_uid, effective_uid, real_gid, effective_gid in invalid_ids:
            with (
                self.subTest(ids=(real_uid, effective_uid, real_gid, effective_gid)),
                patch.object(daemon.os, "getuid", return_value=real_uid),
                patch.object(daemon.os, "geteuid", return_value=effective_uid),
                patch.object(daemon.os, "getgid", return_value=real_gid),
                patch.object(daemon.os, "getegid", return_value=effective_gid),
                self.assertRaises(daemon.PaperExecutorDaemonError),
            ):
                daemon._validate_service_identity()

    def test_policy_file_rejects_symlink_nonroot_and_owner_writable_file(self) -> None:
        target = self.root / "target.yml"
        self._write_config(target)
        linked = self.root / "linked.yml"
        linked.symlink_to(target)
        mutable = self.root / "mutable.yml"
        self._write_config(mutable)
        mutable.chmod(0o600)

        with patch.object(daemon, "_validate_immutable_parent_chain"):
            with self.assertRaises(daemon.PaperExecutorDaemonError):
                daemon._read_immutable_config(linked)
            with self.assertRaisesRegex(
                daemon.PaperExecutorDaemonError,
                "not immutable and bounded",
            ):
                daemon._read_immutable_config(target)
            with self.assertRaisesRegex(
                daemon.PaperExecutorDaemonError,
                "not immutable and bounded",
            ):
                daemon._read_immutable_config(mutable)

    def test_policy_file_rejects_mutable_parent_chain(self) -> None:
        policy = self.root / "policy.yml"
        self._write_config(policy)

        with self.assertRaisesRegex(
            daemon.PaperExecutorDaemonError,
            "not root-owned and immutable",
        ):
            daemon._read_immutable_config(policy)

    def test_policy_file_change_during_read_is_rejected(self) -> None:
        policy = self.root / "policy.yml"
        self._write_config(policy)
        real_read = daemon.os.read
        changed = False

        def changed_read(descriptor: int, size: int) -> bytes:
            nonlocal changed
            payload = real_read(descriptor, size)
            if not changed:
                changed = True
                policy.chmod(0o600)
                with policy.open("ab") as stream:
                    stream.write(b"!")
            return payload

        with (
            patch.object(daemon, "_validate_immutable_parent_chain"),
            patch.object(daemon, "_service_can_replace_or_write", return_value=False),
            patch.object(daemon.os, "read", side_effect=changed_read),
            self.assertRaisesRegex(
                daemon.PaperExecutorDaemonError,
                "changed while reading",
            ),
        ):
            daemon._read_immutable_config(policy)

    def test_mutable_policy_rejects_runtime_before_any_external_constructor(self) -> None:
        risk = self.root / "risk.yml"
        universe = self.root / "universe.yml"
        self._write_config(risk)
        self._write_config(universe)
        risk.chmod(0o600)

        with (
            patch.object(daemon, "_validate_service_identity"),
            patch.object(daemon, "_validate_immutable_parent_chain"),
            patch.object(daemon, "load_risk_config_bytes") as load_risk,
            patch.object(daemon, "load_universe_config_bytes") as load_universe,
            patch.object(daemon, "load_systemd_credentials") as load_credentials,
            patch.object(
                daemon,
                "build_exclusive_alpaca_paper_client",
            ) as build_client,
            patch.object(daemon, "build_alpaca_market_data_client") as build_market,
            patch.object(
                daemon,
                "build_alpaca_crypto_market_data_client",
            ) as build_crypto,
            patch.object(daemon, "AlpacaPaperBroker") as build_broker,
            self.assertRaises(daemon.PaperExecutorDaemonError),
        ):
            daemon.build_daemon_runtime(
                risk_config=risk,
                universe_configs=(universe,),
                authz_config=self.root / "authz.yml",
                socket_path=self.root / "executor.sock",
                supervisor_root=self.root / "supervisor",
                env={},
            )

        for external_call in (
            load_risk,
            load_universe,
            load_credentials,
            build_client,
            build_market,
            build_crypto,
            build_broker,
        ):
            external_call.assert_not_called()

    def test_policy_bundle_parses_the_exact_once_read_bytes_before_credentials(
        self,
    ) -> None:
        risk = self.root / "risk.yml"
        universe = self.root / "universe.yml"
        authz = self.root / "authz.yml"
        risk_payload = b"sealed-risk"
        universe_payload = b"sealed-universe"
        authz_payload = b"sealed-authz"

        with (
            patch.object(daemon, "_validate_service_identity"),
            patch.object(
                daemon,
                "_read_immutable_config",
                side_effect=(risk_payload, universe_payload, authz_payload),
            ) as read_policy,
            patch.object(
                daemon,
                "_policy_bundle_sha256",
                return_value="a" * 64,
            ) as hash_policy,
            patch.object(
                daemon,
                "load_risk_config_bytes",
                return_value=object(),
            ) as load_risk,
            patch.object(
                daemon,
                "load_universe_config_bytes",
                side_effect=ValueError("invalid sealed universe"),
            ) as load_universe,
            patch.object(
                daemon,
                "load_executor_authorization_policy_bytes",
            ) as load_authz,
            patch.object(daemon, "load_systemd_credentials") as load_credentials,
            patch.object(
                daemon,
                "build_exclusive_alpaca_paper_client",
            ) as build_client,
            patch.object(daemon, "build_alpaca_market_data_client") as build_market,
            patch.object(
                daemon,
                "build_alpaca_crypto_market_data_client",
            ) as build_crypto,
            patch.object(daemon, "AlpacaPaperBroker") as build_broker,
            self.assertRaisesRegex(ValueError, "invalid sealed universe"),
        ):
            daemon.build_daemon_runtime(
                risk_config=risk,
                universe_configs=(universe,),
                authz_config=authz,
                socket_path=self.root / "executor.sock",
                supervisor_root=self.root / "supervisor",
                env={},
            )

        self.assertEqual(
            read_policy.call_args_list,
            [call(risk), call(universe), call(authz)],
        )
        hash_policy.assert_called_once_with(
            (risk, universe),
            payloads=(risk_payload, universe_payload),
        )
        load_risk.assert_called_once_with(risk_payload)
        load_universe.assert_called_once_with(universe_payload)
        load_authz.assert_not_called()
        for external_call in (
            load_credentials,
            build_client,
            build_market,
            build_crypto,
            build_broker,
        ):
            external_call.assert_not_called()

    def test_runtime_preserves_exact_policy_symbols_without_alias_expansion(
        self,
    ) -> None:
        risk = self.root / "risk.yml"
        crypto = self.root / "crypto.yml"
        equities = self.root / "equities.yml"
        authz = self.root / "authz.yml"
        authority = SimpleNamespace(
            account_id=TEST_ACCOUNT_ID,
            executor_journal_path=self.root / "executor.sqlite3",
        )
        application = Mock()
        server = Mock()
        authorization_policy = SimpleNamespace(
            policy_sha256="f" * 64,
            socket_gid=1300,
            admit=Mock(),
            require=Mock(),
        )

        with (
            patch.object(daemon, "_validate_service_identity"),
            patch.object(
                daemon,
                "_read_immutable_config",
                side_effect=(b"risk", b"crypto", b"equities", b"authz"),
            ),
            patch.object(
                daemon,
                "_policy_bundle_sha256",
                return_value="a" * 64,
            ),
            patch.object(daemon, "load_risk_config_bytes", return_value=object()),
            patch.object(
                daemon,
                "load_universe_config_bytes",
                side_effect=(
                    SimpleNamespace(symbols=("BTC/USD",)),
                    SimpleNamespace(symbols=("SPY", "BTC/USD")),
                ),
            ),
            patch.object(
                daemon,
                "load_executor_authorization_policy_bytes",
                return_value=authorization_policy,
            ),
            patch.object(
                daemon,
                "load_systemd_credentials",
                return_value=daemon.DaemonCredentials(
                    api_key=TEST_API_KEY,
                    secret_key=TEST_SECRET_KEY,
                ),
            ),
            patch.object(
                daemon,
                "build_exclusive_alpaca_paper_client",
                return_value=authority,
            ),
            patch.object(daemon, "build_alpaca_market_data_client"),
            patch.object(daemon, "build_alpaca_crypto_market_data_client"),
            patch.object(daemon, "AlpacaPaperBroker", return_value=Mock()) as broker,
            patch.object(daemon, "DurableExecutorCommandJournal"),
            patch.object(
                daemon,
                "PaperExecutorApplication",
                return_value=application,
            ),
            patch.object(daemon, "PaperExecutorServer", return_value=server),
        ):
            runtime = daemon.build_daemon_runtime(
                risk_config=risk,
                universe_configs=(crypto, equities),
                authz_config=authz,
                socket_path=self.root / "executor.sock",
                supervisor_root=self.root / "supervisor",
                env={},
            )

        self.assertEqual(broker.call_args.kwargs["allowlist"], ("BTC/USD", "SPY"))
        self.assertNotIn("BTCUSD", broker.call_args.kwargs["allowlist"])
        application.start.assert_called_once_with()
        self.assertIs(runtime.application, application)
        self.assertIs(runtime.server, server)


class RunDaemonTests(unittest.TestCase):
    def test_ready_and_stopping_notifications_wrap_clean_shutdown(self) -> None:
        application = Mock()
        application.run_state = ExecutorRunState.READY
        server = Mock()
        stopped = Event()
        stopped.set()
        runtime = daemon.DaemonRuntime(application=application, server=server)

        with patch.object(daemon, "_systemd_notify") as notify:
            result = daemon.run_daemon(runtime, stop_event=stopped)

        self.assertEqual(result, 0)
        self.assertEqual(
            notify.call_args_list,
            [
                call("READY=1\nSTATUS=paper executor serving"),
                call("STOPPING=1\nSTATUS=paper executor stopping"),
            ],
        )
        server.start.assert_called_once_with()
        server.serve_once.assert_not_called()
        server.close.assert_called_once_with()
        application.close.assert_called_once_with()

    def test_blocked_recovery_obeys_backoff_before_retry(self) -> None:
        application = Mock()
        application.run_state = ExecutorRunState.BLOCKED
        reports = iter((SimpleNamespace(pending=1), SimpleNamespace(pending=0)))

        def recover_once() -> SimpleNamespace:
            report = next(reports)
            if not report.pending:
                application.run_state = ExecutorRunState.READY
            return report

        application.recover_once.side_effect = recover_once
        server = Mock()
        stopped = Mock()
        stopped.is_set.side_effect = (False, False, False, True)
        runtime = daemon.DaemonRuntime(application=application, server=server)

        with (
            patch.object(daemon, "_systemd_notify") as notify,
            patch.object(
                daemon.time,
                "monotonic",
                side_effect=(0.0, 0.0, 1.0, 2.0),
            ),
            patch.object(daemon, "_jittered_delay", return_value=2.0) as jitter,
        ):
            result = daemon.run_daemon(runtime, stop_event=stopped)

        self.assertEqual(result, 0)
        self.assertEqual(application.recover_once.call_count, 2)
        self.assertEqual(server.serve_once.call_count, 3)
        jitter.assert_called_once_with(2.0)
        self.assertEqual(
            notify.call_args_list,
            [
                call("READY=1\nSTATUS=paper executor serving"),
                call("STATUS=paper executor recovery complete"),
                call("STOPPING=1\nSTATUS=paper executor stopping"),
            ],
        )
        server.close.assert_called_once_with()
        application.close.assert_called_once_with()

    def test_unexpected_serve_exception_still_closes_server_and_application(self) -> None:
        application = Mock()
        application.run_state = ExecutorRunState.READY
        server = Mock()
        server.serve_once.side_effect = RuntimeError("serve failed")
        stopped = Mock()
        stopped.is_set.return_value = False
        runtime = daemon.DaemonRuntime(application=application, server=server)

        with (
            patch.object(daemon, "_systemd_notify") as notify,
            self.assertRaisesRegex(RuntimeError, "serve failed"),
        ):
            daemon.run_daemon(runtime, stop_event=stopped)

        self.assertEqual(
            notify.call_args_list,
            [
                call("READY=1\nSTATUS=paper executor serving"),
                call("STOPPING=1\nSTATUS=paper executor stopping"),
            ],
        )
        server.close.assert_called_once_with()
        application.close.assert_called_once_with()

    def test_server_close_failure_still_closes_application(self) -> None:
        application = Mock()
        application.run_state = ExecutorRunState.READY
        server = Mock()
        server.close.side_effect = RuntimeError("server close failed")
        stopped = Event()
        stopped.set()
        runtime = daemon.DaemonRuntime(application=application, server=server)

        with (
            patch.object(daemon, "_systemd_notify") as notify,
            self.assertRaisesRegex(RuntimeError, "server close failed"),
        ):
            daemon.run_daemon(runtime, stop_event=stopped)

        self.assertEqual(
            notify.call_args_list,
            [
                call("READY=1\nSTATUS=paper executor serving"),
                call("STOPPING=1\nSTATUS=paper executor stopping"),
            ],
        )
        server.close.assert_called_once_with()
        application.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
