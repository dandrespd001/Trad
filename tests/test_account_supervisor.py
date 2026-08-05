import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trading_ai.execution.account_supervisor import (
    AccountLeaseBusyError,
    AccountLeaseIntegrityError,
    AccountLeaseNotActiveError,
    AccountMutationLease,
    InvalidAccountScopeError,
    account_scope_sha256,
)


class AccountMutationLeaseTests(unittest.TestCase):
    def test_exclusive_lease_has_stable_journal_and_increments_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            first = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=root,
                owner_id="worker-a",
            )
            first_journal = first.journal_path
            self.assertEqual(first.fence.epoch, 1)
            self.assertEqual(first_journal.parent, root)
            self.assertEqual(first.lock_path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(AccountLeaseBusyError):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                    owner_id="worker-b",
                )
            first.release()

            second = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=root,
                owner_id="worker-b",
            )
            try:
                self.assertEqual(second.fence.epoch, 2)
                self.assertEqual(second.journal_path, first_journal)
            finally:
                second.release()

    def test_different_accounts_use_independent_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            first = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=root,
            )
            second = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-2",
                root=root,
            )
            try:
                self.assertNotEqual(first.fence.scope_sha256, second.fence.scope_sha256)
            finally:
                second.release()
                first.release()

    def test_mutation_validates_scope_and_rejects_nested_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lease = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=Path(tmp) / "supervisor",
            )
            try:
                with lease.mutation(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    operation="submit",
                    mutation_id="order-1",
                ) as fence:
                    self.assertEqual(fence.epoch, 1)
                    with self.assertRaises(AccountLeaseBusyError), lease.mutation(
                        broker="alpaca",
                        environment="paper",
                        account_id="paper-account-1",
                        operation="cancel",
                        mutation_id="order-2",
                    ):
                        pass
                with self.assertRaises(InvalidAccountScopeError):
                    lease.validate(
                        broker="alpaca",
                        environment="paper",
                        account_id="another-account",
                    )
            finally:
                lease.release()

    def test_live_or_malformed_scope_is_rejected(self) -> None:
        with self.assertRaises(InvalidAccountScopeError):
            account_scope_sha256(broker="alpaca", environment="live", account_id="a")
        with self.assertRaises(InvalidAccountScopeError):
            account_scope_sha256(broker="alpaca", environment="paper", account_id="")
        with self.assertRaises(InvalidAccountScopeError):
            account_scope_sha256(broker="alpaca", environment="paper", account_id=True)  # type: ignore[arg-type]

    def test_tampered_payload_fails_closed_and_release_unlocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            lease = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=root,
            )
            payload = json.loads(lease.lock_path.read_text(encoding="utf-8"))
            payload["schema_version"] = 999
            lease.lock_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(AccountLeaseIntegrityError):
                lease.validate(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                )
            with self.assertRaises(AccountLeaseIntegrityError):
                lease.release()

            with self.assertRaises(AccountLeaseIntegrityError):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                )

    def test_existing_empty_lease_is_corruption_and_never_resets_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            first = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=root,
            )
            self.assertEqual(first.fence.epoch, 1)
            lock_path = first.lock_path
            first.release()

            lock_path.write_bytes(b"")
            with self.assertRaisesRegex(
                AccountLeaseIntegrityError,
                "cannot be treated as genesis",
            ):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                )

            self.assertEqual(lock_path.read_bytes(), b"")

    def test_failed_initial_metadata_write_leaves_fail_closed_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            with (
                mock.patch.object(os, "write", side_effect=OSError("injected disk failure")),
                self.assertRaisesRegex(
                    AccountLeaseIntegrityError,
                    "cannot persist account lease metadata",
                ),
            ):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                )

            with self.assertRaisesRegex(
                AccountLeaseIntegrityError,
                "cannot be treated as genesis",
            ):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                )

    def test_released_lease_cannot_be_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lease = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
                root=Path(tmp) / "supervisor",
            )
            lease.release()

    def test_release_cannot_close_the_lease_during_a_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            lease = AccountMutationLease.acquire(
                broker="alpaca",
                environment="paper",
                account_id="paper-account",
                root=Path(temp_dir) / "supervisor",
            )

            with lease.mutation(
                broker="alpaca",
                environment="paper",
                account_id="paper-account",
                operation="submit",
                mutation_id="order-1",
            ):
                with self.assertRaises(AccountLeaseBusyError):
                    lease.release()
                self.assertTrue(lease.active)

            lease.release()
            self.assertFalse(lease.active)
            with self.assertRaises(AccountLeaseNotActiveError):
                lease.validate(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                )

    def test_unsafe_root_permissions_and_symlink_lock_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            root.mkdir(mode=0o755)
            os.chmod(root, 0o755)  # noqa: S103 - intentionally unsafe fixture
            with self.assertRaises(AccountLeaseIntegrityError):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "supervisor"
            root.mkdir(mode=0o700)
            scope = account_scope_sha256(
                broker="alpaca",
                environment="paper",
                account_id="paper-account-1",
            )
            target = root / "target"
            target.write_text("", encoding="utf-8")
            (root / f"{scope}.lease").symlink_to(target)
            with self.assertRaises(AccountLeaseIntegrityError):
                AccountMutationLease.acquire(
                    broker="alpaca",
                    environment="paper",
                    account_id="paper-account-1",
                    root=root,
                )


if __name__ == "__main__":
    unittest.main()
