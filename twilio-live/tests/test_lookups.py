"""Pure tests for read-only lookup formatting and guards."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lookups  # noqa: E402


OCT_29 = datetime(2026, 10, 29, 21, 24, tzinfo=timezone.utc)
NOV_7 = datetime(2026, 11, 7, 5, 9, tzinfo=timezone.utc)


class FormatTests(unittest.TestCase):
    def test_cents_to_dollars(self) -> None:
        self.assertEqual(lookups.cents_to_dollars(0), "$0.00")
        self.assertEqual(lookups.cents_to_dollars(1), "$0.01")
        self.assertEqual(lookups.cents_to_dollars(10), "$0.10")
        self.assertEqual(lookups.cents_to_dollars(199), "$1.99")
        self.assertEqual(lookups.cents_to_dollars(570000), "$5700.00")
        self.assertEqual(lookups.cents_to_dollars(910000), "$9100.00")
        self.assertEqual(lookups.cents_to_dollars(-50), "-$0.50")
        self.assertEqual(lookups.format_money(910000, "usd"), "$9100.00 USD")
        self.assertEqual(lookups.format_money(500, "jpy"), "500 JPY")

    def test_eastern_dates_include_zone_label(self) -> None:
        self.assertEqual(lookups.format_eastern(NOV_7), "November 7, 2026, 12:09 AM ET")
        self.assertEqual(lookups.format_eastern(OCT_29), "October 29, 2026, 5:24 PM ET")

    def test_stripe_lists_convert_cents_and_label_eastern_dates(self) -> None:
        text = lookups.format_stripe_lists(
            {
                "data": [
                    {
                        "id": "ch_moe",
                        "amount": 570000,
                        "amount_refunded": 0,
                        "currency": "usd",
                        "status": "succeeded",
                        "created": int(OCT_29.timestamp()),
                        "description": "Front end",
                        "billing_details": {"name": "MoeModz LLC"},
                        "receipt_email": "moe@example.com",
                    }
                ]
            },
            {
                "data": [
                    {
                        "id": "in_red",
                        "number": "NSA-14",
                        "amount_due": 910000,
                        "amount_paid": 0,
                        "amount_remaining": 910000,
                        "currency": "usd",
                        "status": "open",
                        "created": int(NOV_7.timestamp()),
                        "customer_name": "Red",
                        "customer_email": "shop@example.com",
                        "custom_fields": [
                            {"name": "Request", "value": "NSA-14"},
                            {"name": "VIN", "value": "WBS83CH04NCK94954"},
                        ],
                    }
                ]
            },
        )
        self.assertIn("$5700.00 USD", text)
        self.assertIn("October 29, 2026, 5:24 PM ET", text)
        self.assertIn("$9100.00 USD", text)
        self.assertIn("November 7, 2026, 12:09 AM ET", text)
        self.assertIn("NSA-14", text)
        self.assertIn("America/New_York", text)
        self.assertNotIn("570000", text)
        self.assertNotIn("910000", text)

    def test_admin_requests_are_newest_first_and_skip_trash(self) -> None:
        text = lookups.format_admin_inquiries(
            {
                "inquiries": [
                    {
                        "id": 3,
                        "name": "Older",
                        "requestedPart": "Old bumper",
                        "status": "completed",
                        "createdAt": "2026-10-01 15:00:00",
                        "sellingPrice": 1000,
                    },
                    {
                        "id": 24,
                        "name": "Oreste",
                        "requestedPart": "G30 front end",
                        "status": "paid",
                        "createdAt": "2026-11-01T20:02:00Z",
                        "sellingPrice": 5700,
                        "vin": "WBA123",
                    },
                    {
                        "id": 30,
                        "name": "Trash",
                        "requestedPart": "discarded",
                        "status": "new",
                        "createdAt": "2026-11-06 12:00:00",
                        "deletedAt": "2026-11-06 12:01:00",
                    },
                ]
            },
            limit=5,
        )
        self.assertLess(text.index("Request #24"), text.index("Request #3"))
        self.assertNotIn("Trash", text)
        self.assertIn("Paid in full", text)
        self.assertIn("$5700.00 USD", text)
        self.assertIn("November 1, 2026, 3:02 PM ET", text)
        self.assertIn("VIN WBA123", text)

    def test_stop_and_write_detectors(self) -> None:
        self.assertTrue(lookups.is_stop_talking("quit talking"))
        self.assertTrue(lookups.is_stop_talking("Please stop talking about that."))
        self.assertTrue(lookups.is_stop_talking("Stop."))
        self.assertFalse(lookups.is_stop_talking("stop the order"))
        self.assertTrue(lookups.is_write_request("Refund the deposit"))
        self.assertTrue(lookups.is_write_request("Can you refund Moe"))
        self.assertTrue(lookups.is_write_request("Email the customer a pickup time"))
        self.assertTrue(lookups.is_write_request("pay the vendor"))
        self.assertFalse(lookups.is_write_request("Please check the open orders"))
        self.assertFalse(lookups.is_write_request("Did the refund post"))
        self.assertFalse(lookups.is_write_request("Send me the numbers"))
        self.assertFalse(lookups.is_write_request("What payments came in"))

    def test_chat_payload_sends_read_only_tools(self) -> None:
        payload = lookups.chat_payload(
            "grok-test",
            [{"role": "user", "content": "hi"}],
            tools=lookups.READ_TOOLS,
            max_tokens=180,
        )
        self.assertEqual(payload["tool_choice"], "auto")
        names = [tool["function"]["name"] for tool in payload["tools"]]
        self.assertEqual(names, ["stripe_recent_payments", "admin_requests", "ask_team"])
        bare = lookups.chat_payload("grok-test", [], tools=None, max_tokens=20)
        self.assertNotIn("tools", bare)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in ("STRIPE_RESTRICTED_KEY", "ADMIN_READ_TOKEN")
        }

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def test_stripe_refuses_secret_keys_and_write_paths(self) -> None:
        os.environ["STRIPE_RESTRICTED_KEY"] = "sk_test_not_restricted"
        called = False

        async def explode(path, params):
            nonlocal called
            called = True
            raise AssertionError("stripe_get should not run")

        with unittest.mock.patch.object(lookups, "stripe_get", explode):
            text = await lookups.stripe_recent_payments()
        self.assertFalse(called)
        self.assertIn("not configured", text)
        self.assertIn("Do not invent", text)

        os.environ["STRIPE_RESTRICTED_KEY"] = "rk_test_read"
        with self.assertRaises(lookups.LookupError):
            await lookups.stripe_get("/v1/refunds", {})

    async def test_stripe_and_admin_format_live_payloads(self) -> None:
        os.environ["STRIPE_RESTRICTED_KEY"] = "rk_test_read"
        os.environ["ADMIN_READ_TOKEN"] = "admin-read"

        async def fake_stripe(path, params):
            self.assertEqual(params["limit"], 2)
            if path == "/v1/charges":
                return {
                    "data": [
                        {
                            "id": "ch_1",
                            "amount": 2500,
                            "currency": "usd",
                            "status": "succeeded",
                            "created": int(NOV_7.timestamp()),
                        }
                    ]
                }
            self.assertEqual(path, "/v1/invoices")
            return {"data": []}

        async def fake_admin():
            return {
                "inquiries": [
                    {
                        "id": 8,
                        "name": "Red",
                        "requestedPart": "Radiator",
                        "status": "new",
                        "createdAt": "2026-11-07T05:09:00Z",
                    }
                ]
            }

        with unittest.mock.patch.object(lookups, "stripe_get", fake_stripe):
            stripe_text = await lookups.stripe_recent_payments(2)
        self.assertIn("$25.00 USD", stripe_text)
        self.assertIn("November 7, 2026, 12:09 AM ET", stripe_text)

        with unittest.mock.patch.object(lookups, "admin_get", fake_admin):
            admin_text = await lookups.admin_requests(2)
        self.assertIn("Request #8", admin_text)
        self.assertIn("New request", admin_text)
        self.assertIn("Red", admin_text)

    async def test_unknown_tool_is_refused(self) -> None:
        text = await lookups.run_read_tool("refund_charge", {})
        self.assertIn("not available", text)
        self.assertIn("approval", text)


if __name__ == "__main__":
    unittest.main()
