from __future__ import annotations

import unittest
from datetime import UTC, datetime

from trading_ai.execution.alpaca_paper import (
    ACTIVITY_PAGE_SIZE,
    AlpacaPaperBroker,
    IncompleteFillActivitySnapshotError,
    InvalidFillActivitySnapshotError,
)
from trading_ai.risk.policy import RiskLimits


class _ActivitiesClient:
    def __init__(self, pages: list[object]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, path: str, params: dict[str, object]) -> object:
        self.calls.append((path, dict(params)))
        if not self.pages:
            return []
        return self.pages.pop(0)


def _activity(
    activity_id: str,
    *,
    order_id: str = "order-1",
    qty: object = "1",
    price: object = "100.25",
) -> dict[str, object]:
    return {
        "id": activity_id,
        "order_id": order_id,
        "symbol": "SPY",
        "side": "buy",
        "qty": qty,
        "price": price,
        "transaction_time": "2026-07-14T13:30:00.250Z",
        "cum_qty": qty,
        "leaves_qty": "0",
        "type": "fill",
        "order_status": "filled",
    }


def _broker(client: object) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        client=client,
        allowlist=("SPY",),
        risk_limits=RiskLimits(max_drawdown_pct=0.10),
        dry_run=False,
    )


class FillActivityPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.after = datetime(2026, 7, 14, tzinfo=UTC)
        self.until = datetime(2026, 7, 15, tzinfo=UTC)

    def test_reads_and_validates_individual_fill_activity(self) -> None:
        client = _ActivitiesClient([[_activity("activity-1")]])
        activities = _broker(client).list_fill_activities(after=self.after, until=self.until)
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].activity_id, "activity-1")
        self.assertEqual(activities[0].price, 100.25)
        self.assertEqual(client.calls[0][0], "/account/activities/FILL")
        self.assertNotIn("page_token", client.calls[0][1])

    def test_full_page_uses_last_opaque_id_as_next_page_token(self) -> None:
        first = [_activity(f"activity-{index:03d}") for index in range(ACTIVITY_PAGE_SIZE)]
        second = [_activity("activity-final", order_id="order-2")]
        client = _ActivitiesClient([first, second])
        activities = _broker(client).list_fill_activities(after=self.after, until=self.until)
        self.assertEqual(len(activities), ACTIVITY_PAGE_SIZE + 1)
        self.assertEqual(
            client.calls[1][1]["page_token"],
            first[-1]["id"],
        )

    def test_conflicting_replay_is_blocked(self) -> None:
        first = [_activity(f"activity-{index:03d}") for index in range(ACTIVITY_PAGE_SIZE)]
        conflict = _activity("activity-000", price="101")
        client = _ActivitiesClient([first, [conflict]])
        with self.assertRaises(InvalidFillActivitySnapshotError):
            _broker(client).list_fill_activities(after=self.after, until=self.until)

    def test_missing_activity_id_and_non_list_page_are_blocked(self) -> None:
        full_page = [_activity(f"activity-{index:03d}") for index in range(ACTIVITY_PAGE_SIZE)]
        full_page[-1]["id"] = ""
        with self.assertRaises(InvalidFillActivitySnapshotError):
            _broker(_ActivitiesClient([full_page])).list_fill_activities(
                after=self.after,
                until=self.until,
            )
        with self.assertRaises(IncompleteFillActivitySnapshotError):
            _broker(_ActivitiesClient([{"unexpected": "mapping"}])).list_fill_activities(
                after=self.after,
                until=self.until,
            )

    def test_invalid_numeric_and_timestamp_fields_are_blocked(self) -> None:
        invalid = (
            {**_activity("a"), "qty": "nan"},
            {**_activity("a"), "price": "0"},
            {**_activity("a"), "transaction_time": "2026-07-14T13:30:00"},
            {**_activity("a"), "cum_qty": "0.5"},
        )
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(InvalidFillActivitySnapshotError):
                _broker(_ActivitiesClient([[row]])).list_fill_activities(
                    after=self.after,
                    until=self.until,
                )

    def test_naive_or_reversed_window_is_rejected_before_api_call(self) -> None:
        broker = _broker(_ActivitiesClient([]))
        with self.assertRaises(ValueError):
            broker.list_fill_activities(
                after=datetime(2026, 7, 14),
                until=self.until,
            )
        with self.assertRaises(ValueError):
            broker.list_fill_activities(after=self.until, until=self.after)


if __name__ == "__main__":
    unittest.main()
