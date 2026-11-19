# Copyright 2026 the google-ads-mcp fork contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test cases for the aggregate tool."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import aggregate as agg
from tests import fake_stream

CURRENCY_QUERY = "SELECT customer.currency_code FROM customer"


def term_row(term, ad_group, cost, clicks, conversions, impressions=100):
    """A search_term_view row shaped like a GoogleAdsRow."""
    return NS(
        search_term_view=NS(search_term=term),
        ad_group=NS(name=ad_group),
        metrics=NS(
            cost_micros=cost,
            clicks=clicks,
            conversions=conversions,
            impressions=impressions,
        ),
    )


def fake_service(rows_by_customer, currency_by_customer=None):
    """A GoogleAdsService whose stream yields rows in batches of two."""
    service = MagicMock()

    def search_stream(customer_id, query):
        if query == CURRENCY_QUERY:
            code = (currency_by_customer or {})[customer_id]
            return [NS(results=[NS(customer=NS(currency_code=code))])]
        rows = rows_by_customer[customer_id]
        if isinstance(rows, Exception):
            raise rows
        return [NS(results=rows[i : i + 2]) for i in range(0, len(rows), 2)]

    service.search_stream.side_effect = search_stream
    return service


class TestAggregate(unittest.TestCase):
    """Test cases for the aggregate tool."""

    def setUp(self):
        agg._currency_cache.clear()
        patcher = patch("ads_mcp.utils.get_googleads_service")
        self.get_service = patcher.start()
        self.addCleanup(patcher.stop)
        reader = patch(
            "ads_mcp.streaming.search_stream", fake_stream.search_stream
        )
        reader.start()
        self.addCleanup(reader.stop)

    def serve(self, rows_by_customer, currency_by_customer=None):
        service = fake_service(rows_by_customer, currency_by_customer)
        self.get_service.return_value = service
        return service

    def test_builds_query_without_order_or_limit(self):
        """Tests that grouping happens here, so GAQL gets no ORDER BY/LIMIT."""
        service = self.serve({"1": []})

        output = agg.aggregate(
            customer_ids=["1"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.cost_micros", "metrics.conversions"],
            conditions=["segments.date DURING LAST_30_DAYS"],
        )

        expected = (
            "SELECT search_term_view.search_term,metrics.cost_micros,"
            "metrics.conversions FROM search_term_view "
            "WHERE segments.date DURING LAST_30_DAYS "
            "PARAMETERS omit_unselected_resource_names=true"
        )
        service.search_stream.assert_called_once_with(
            customer_id="1", query=expected
        )
        self.assertEqual(output["query"], expected)
        self.assertEqual(output["groups"], [])

    def test_change_event_gets_its_required_limit(self):
        """Tests that change_event is read newest first with a LIMIT."""
        service = self.serve({"1": []})

        output = agg.aggregate(
            customer_ids=["1"],
            resource="change_event",
            group_by=["change_event.user_email"],
            conditions=["change_event.change_date_time DURING LAST_7_DAYS"],
        )

        expected = (
            "SELECT change_event.user_email FROM change_event "
            "WHERE change_event.change_date_time DURING LAST_7_DAYS "
            "ORDER BY change_event.change_date_time DESC LIMIT 10000 "
            "PARAMETERS omit_unselected_resource_names=true"
        )
        service.search_stream.assert_called_once_with(
            customer_id="1", query=expected
        )
        self.assertNotIn("incomplete", output)

    def test_reports_customers_that_reached_the_required_limit(self):
        """Tests that a capped customer is named in `incomplete`."""
        self.serve({"1": [], "2": []})
        scans = {"1": ({("a",): [("a",), 3]}, 3), "2": ({}, 1)}

        with (
            patch.dict(agg._REQUIRED_LIMITS, {"change_event": (3, "x")}),
            patch.object(
                agg, "_scan_customer", side_effect=lambda cid, *_: scans[cid]
            ),
        ):
            output = agg.aggregate(
                customer_ids=["1", "2"],
                resource="change_event",
                group_by=["change_event.user_email"],
            )

        self.assertEqual(output["rows_scanned"], 4)
        self.assertIn("customer(s) 1 reached", output["incomplete"])

    def test_logs_a_failed_line_when_the_group_limit_ends_a_scan(self):
        """Tests that hitting the group limit is logged, not silent."""
        rows = [
            NS(campaign=NS(name=name), metrics=NS(clicks=1))
            for name in ("a", "b", "c")
        ]
        self.serve({"1": rows})

        with (
            patch.object(agg, "_MAX_GROUPS", 2),
            self.assertLogs(agg.utils.logger, "WARNING") as logs,
            self.assertRaises(ToolError),
        ):
            agg.aggregate(
                customer_ids=["1"],
                resource="campaign",
                group_by=["campaign.name"],
                metrics=["metrics.clicks"],
            )

        self.assertIn("ads_mcp.aggregate failed [", logs.output[0])
        self.assertIn("(after 3 row(s), 2 group(s))", logs.output[0])

    def test_sums_per_group_and_recomputes_ratios(self):
        """Tests sums per group and ratios from sums, not averaged ratios."""
        self.serve(
            {
                "1": [
                    term_row("cars", "A", 1_000_000, 1, 0, impressions=10),
                    term_row("cars", "B", 9_000_000, 99, 2, impressions=990),
                    term_row("vans", "A", 500_000, 5, 1, impressions=50),
                ]
            }
        )

        output = agg.aggregate(
            customer_ids=["1"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=[
                "metrics.cost_micros",
                "metrics.clicks",
                "metrics.impressions",
                "metrics.conversions",
            ],
        )

        cars = output["groups"][0]
        self.assertEqual(cars["search_term_view.search_term"], "cars")
        self.assertEqual(cars["row_count"], 2)
        self.assertEqual(cars["metrics.cost_micros"], 10_000_000)
        self.assertEqual(cars["metrics.clicks"], 100)
        # 100 clicks / 1000 impressions, not the mean of 0.1 and 0.1.
        self.assertEqual(cars["derived.ctr"], 0.1)
        self.assertEqual(cars["derived.average_cpc_micros"], 100_000)
        self.assertEqual(cars["derived.cost_per_conversion_micros"], 5_000_000)
        self.assertEqual(output["rows_scanned"], 3)
        self.assertEqual(output["groups_found"], 2)
        self.assertEqual(output["totals"][0]["metrics.cost_micros"], 10_500_000)

    def test_ratio_with_zero_denominator_is_null(self):
        """Tests that an undefined ratio is null rather than a crash."""
        self.serve({"1": [term_row("cars", "A", 1_000_000, 3, 0)]})

        output = agg.aggregate(
            customer_ids=["1"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.cost_micros", "metrics.conversions"],
        )

        self.assertIsNone(
            output["groups"][0]["derived.cost_per_conversion_micros"]
        )

    def test_having_filters_on_group_totals(self):
        """Tests that a term converting in another ad group is not 'wasteful'."""
        self.serve(
            {
                "1": [
                    term_row("cars", "A", 4_000_000, 4, 0),
                    term_row("cars", "B", 1_000_000, 1, 1),
                    term_row("loans", "A", 3_000_000, 6, 0),
                    term_row("loans", "B", 2_000_000, 2, 0),
                    term_row("cheap", "A", 100_000, 1, 0),
                ]
            }
        )

        output = agg.aggregate(
            customer_ids=["1"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.cost_micros", "metrics.conversions"],
            having=["metrics.conversions = 0", "metrics.cost_micros > 1000000"],
        )

        self.assertEqual(
            [g["search_term_view.search_term"] for g in output["groups"]],
            ["loans"],
        )
        self.assertEqual(output["groups_found"], 3)
        self.assertEqual(output["groups_matched"], 1)
        self.assertEqual(output["totals"][0]["metrics.cost_micros"], 5_000_000)

    def test_order_by_and_top(self):
        """Tests sorting, the top cap, and that totals cover all matches."""
        self.serve(
            {
                "1": [
                    term_row("a", "A", 1, 10, 0),
                    term_row("b", "A", 3, 30, 0),
                    term_row("c", "A", 2, 20, 0),
                ]
            }
        )

        output = agg.aggregate(
            customer_ids=["1"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.cost_micros", "metrics.clicks"],
            order_by="metrics.clicks ASC",
            top=2,
        )

        self.assertEqual(
            [g["search_term_view.search_term"] for g in output["groups"]],
            ["a", "c"],
        )
        self.assertEqual(output["groups_matched"], 3)
        self.assertEqual(output["groups_returned"], 2)
        self.assertEqual(output["totals"][0]["metrics.clicks"], 60)

    def test_distinct_values_without_metrics(self):
        """Tests the label lookup: distinct campaigns, ordered by row count."""
        rows = [NS(campaign=NS(id=1, name="One"))] * 3 + [
            NS(campaign=NS(id=2, name="Two"))
        ]
        self.serve({"1": rows})

        output = agg.aggregate(
            customer_ids=["1"],
            resource="ad_group_ad_label",
            group_by=["campaign.id", "campaign.name"],
            conditions=["label.id = 123"],
        )

        self.assertEqual(
            output["groups"],
            [
                {"campaign.id": 1, "campaign.name": "One", "row_count": 3},
                {"campaign.id": 2, "campaign.name": "Two", "row_count": 1},
            ],
        )

    def test_counts_rows_when_nothing_is_selected(self):
        """Tests a plain row count selects the resource name."""
        service = self.serve({"1": [NS(), NS(), NS()]})

        output = agg.aggregate(customer_ids=["1"], resource="ad_group_ad")

        query = service.search_stream.call_args.kwargs["query"]
        self.assertTrue(
            query.startswith("SELECT ad_group_ad.resource_name FROM")
        )
        self.assertEqual(output["groups"], [{"row_count": 3}])

    def test_repeated_field_values_can_be_grouped(self):
        """Tests that list values (e.g. final_urls) work as a group key."""
        rows = [
            NS(ad=NS(final_urls=["https://a"])),
            NS(ad=NS(final_urls=["https://a"])),
            NS(ad=NS(final_urls=["https://b"])),
        ]
        self.serve({"1": rows})

        output = agg.aggregate(
            customer_ids=["1"],
            resource="ad_group_ad",
            group_by=["ad.final_urls"],
        )

        self.assertEqual(
            output["groups"][0],
            {"ad.final_urls": ["https://a"], "row_count": 2},
        )

    def test_merges_customers_and_splits_money_by_currency(self):
        """Tests cross-account merging never adds up different currencies."""
        self.serve(
            {
                "1": [term_row("cars", "A", 1_000_000, 1, 0)],
                "2": [term_row("cars", "A", 2_000_000, 2, 0)],
                "3": [term_row("cars", "A", 4_000_000, 4, 0)],
            },
            currency_by_customer={"1": "USD", "2": "USD", "3": "EUR"},
        )

        output = agg.aggregate(
            customer_ids=["1", "2", "3"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.cost_micros", "metrics.clicks"],
        )

        by_currency = {
            g["customer.currency_code"]: g["metrics.cost_micros"]
            for g in output["groups"]
        }
        self.assertEqual(by_currency, {"USD": 3_000_000, "EUR": 4_000_000})
        self.assertEqual(
            sorted(t["customer.currency_code"] for t in output["totals"]),
            ["EUR", "USD"],
        )

    def test_merges_customers_without_currency_when_no_money(self):
        """Tests that counts across accounts are not split by currency."""
        service = self.serve(
            {
                "1": [term_row("cars", "A", 1, 1, 0)],
                "2": [term_row("cars", "A", 1, 2, 0)],
            }
        )

        output = agg.aggregate(
            customer_ids=["1", "2"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.clicks"],
        )

        self.assertEqual(
            output["groups"],
            [
                {
                    "search_term_view.search_term": "cars",
                    "row_count": 2,
                    "metrics.clicks": 3,
                }
            ],
        )
        queries = [
            c.kwargs["query"] for c in service.search_stream.call_args_list
        ]
        self.assertNotIn(CURRENCY_QUERY, queries)

    def test_currency_is_cached_per_customer(self):
        """Tests that currency is looked up once per account per process."""
        service = self.serve(
            {"1": [], "2": []}, currency_by_customer={"1": "USD", "2": "EUR"}
        )
        kwargs = dict(
            customer_ids=["1", "2"],
            resource="campaign",
            metrics=["metrics.cost_micros"],
        )

        agg.aggregate(**kwargs)
        agg.aggregate(**kwargs)

        queries = [
            c.kwargs["query"] for c in service.search_stream.call_args_list
        ]
        self.assertEqual(queries.count(CURRENCY_QUERY), 2)

    def test_customer_failure_lands_in_errors(self):
        """Tests that one failing account does not fail the call."""
        self.serve(
            {
                "1": [term_row("cars", "A", 1, 1, 0)],
                "2": ToolError("[authorization_error.CUSTOMER_NOT_ENABLED]"),
            }
        )

        output = agg.aggregate(
            customer_ids=["1", "2"],
            resource="search_term_view",
            group_by=["search_term_view.search_term"],
            metrics=["metrics.clicks"],
        )

        self.assertEqual(output["groups"][0]["metrics.clicks"], 1)
        self.assertIn("CUSTOMER_NOT_ENABLED", output["errors"]["2"])

    def test_invalid_query_on_canary_fails_fast(self):
        """Tests that a query-structure error stops the fan-out."""
        service = self.serve(
            {
                "1": ToolError("[query_error.UNRECOGNIZED_FIELD] bad"),
                "2": [],
                "3": [],
            }
        )

        with self.assertRaises(ToolError) as context:
            agg.aggregate(
                customer_ids=["1", "2", "3"],
                resource="campaign",
                group_by=["campaign.start_date"],
            )

        self.assertEqual(service.search_stream.call_count, 1)
        self.assertIn("not run against the other 2", str(context.exception))

    def test_api_error_is_enriched_and_stops_a_batch(self):
        """Tests a real GoogleAdsException gets the search tool's enrichment."""
        from google.ads.googleads.errors import GoogleAdsException

        error = MagicMock()
        error.message = "Unrecognized field"
        error.error_code._pb.WhichOneof.return_value = "query_error"
        error.error_code.query_error.name = "UNRECOGNIZED_FIELD"
        error.location.field_path_elements = []
        ex = GoogleAdsException(MagicMock(), MagicMock(), MagicMock(), None)
        ex.failure = MagicMock(errors=[error])
        ex.request_id = "req-9"
        service = self.serve({"1": ex, "2": []})

        with self.assertRaises(ToolError) as context:
            agg.aggregate(
                customer_ids=["1", "2"],
                resource="campaign",
                group_by=["campaign.start_date"],
            )

        message = str(context.exception)
        self.assertIn("[query_error.UNRECOGNIZED_FIELD]", message)
        self.assertIn("Request ID: req-9", message)
        self.assertIn("get_resource_metadata", message)
        self.assertIn("not run against the other 1", message)
        self.assertEqual(service.search_stream.call_count, 1)

    def test_single_customer_error_is_raised_as_is(self):
        """Tests that a one-account call surfaces the original error."""
        self.serve({"1": ToolError("[query_error.UNRECOGNIZED_FIELD] bad")})

        with self.assertRaises(ToolError) as context:
            agg.aggregate(
                customer_ids=["1"],
                resource="campaign",
                group_by=["campaign.start_date"],
            )
        self.assertIn("UNRECOGNIZED_FIELD", str(context.exception))

    def test_every_customer_failed(self):
        """Tests the call fails only when no account succeeded."""
        self.serve({"1": ToolError("boom-1"), "2": ToolError("boom-2")})

        with self.assertRaises(ToolError) as context:
            agg.aggregate(
                customer_ids=["1", "2"],
                resource="campaign",
                group_by=["campaign.id"],
            )
        self.assertIn("Every customer failed", str(context.exception))
        self.assertIn("boom-2", str(context.exception))

    def test_too_many_groups_stops_the_call(self):
        """Tests the group cap aborts instead of rebuilding the row pile."""
        rows = [term_row(f"t{i}", "A", 1, 1, 0) for i in range(5)]
        self.serve({"1": rows})

        with patch.object(agg, "_MAX_GROUPS", 3):
            with self.assertRaises(ToolError) as context:
                agg.aggregate(
                    customer_ids=["1"],
                    resource="search_term_view",
                    group_by=["search_term_view.search_term"],
                )
        self.assertIn("distinct groups", str(context.exception))

    def test_rejects_bad_arguments_before_querying(self):
        """Tests validation errors never reach the API."""
        bad_calls = {
            "non-additive metric": dict(metrics=["metrics.ctr"]),
            "impression share": dict(
                metrics=["metrics.search_impression_share"]
            ),
            "non-metric in metrics": dict(metrics=["campaign.id"]),
            "metric in group_by": dict(group_by=["metrics.clicks"]),
            "partial field name": dict(group_by=["id"]),
            "unknown order_by": dict(
                group_by=["campaign.id"], order_by="metrics.clicks"
            ),
            "derived without parts": dict(
                metrics=["metrics.clicks"], order_by="derived.ctr DESC"
            ),
            "having on a field": dict(
                group_by=["campaign.id"], having=["campaign.id = 1"]
            ),
            "malformed having": dict(
                metrics=["metrics.clicks"], having=["metrics.clicks is big"]
            ),
            "top too large": dict(top=5000),
            "top zero": dict(top=0),
            "no customers": dict(customer_ids=[]),
            "too many customers": dict(
                customer_ids=[str(1000000000 + i) for i in range(51)]
            ),
        }
        for label, overrides in bad_calls.items():
            with self.subTest(label):
                kwargs = dict(customer_ids=["1"], resource="campaign")
                kwargs.update(overrides)
                with self.assertRaises(ToolError):
                    agg.aggregate(**kwargs)
        self.get_service.assert_not_called()

    def test_tool_is_registered_as_read_only(self):
        """Tests aggregate is exposed on the search sub-server."""
        import asyncio

        tool = asyncio.run(agg.search_mcp.get_tool("aggregate"))
        self.assertTrue(tool.annotations.read_only_hint)
        self.assertIn("group_by", tool.parameters["properties"])


if __name__ == "__main__":
    unittest.main()
