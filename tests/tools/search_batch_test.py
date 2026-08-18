# Copyright 2026 Google LLC.
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

"""Test cases for the search_batch tool."""

import unittest
from unittest.mock import call, patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import search


class TestSearchBatch(unittest.TestCase):
    """Test cases for the search_batch tool."""

    @patch("ads_mcp.tools.search.search")
    def test_search_batch_basic(self, mock_search):
        """Tests that each customer is queried once and results are keyed by id."""
        mock_search.side_effect = lambda customer_id, **kwargs: [
            {"customer.id": customer_id}
        ]

        output = search.search_batch(
            customer_ids=["1111111111", "2222222222"],
            fields=["customer.id"],
            resource="customer",
            conditions=["segments.date = '2026-08-15'"],
            orderings=["customer.id ASC"],
            limit=10,
        )

        self.assertEqual(
            output["results"],
            {
                "1111111111": [{"customer.id": "1111111111"}],
                "2222222222": [{"customer.id": "2222222222"}],
            },
        )
        self.assertEqual(output["errors"], {})
        self.assertEqual(mock_search.call_count, 2)
        mock_search.assert_has_calls(
            [
                call(
                    customer_id="1111111111",
                    fields=["customer.id"],
                    resource="customer",
                    conditions=["segments.date = '2026-08-15'"],
                    orderings=["customer.id ASC"],
                    limit=10,
                )
            ],
            any_order=True,
        )

    @patch("ads_mcp.tools.search.search")
    def test_search_batch_deduplicates_ids(self, mock_search):
        """Tests that a repeated customer id is only queried once."""
        mock_search.return_value = []

        output = search.search_batch(
            customer_ids=["1111111111", "1111111111"],
            fields=["customer.id"],
            resource="customer",
        )

        self.assertEqual(mock_search.call_count, 1)
        self.assertEqual(list(output["results"]), ["1111111111"])

    @patch("ads_mcp.tools.search.search")
    def test_search_batch_partial_failure(self, mock_search):
        """Tests that one failing customer lands in errors without failing the batch."""

        def side_effect(customer_id, **kwargs):
            if customer_id == "2222222222":
                raise ToolError("Request ID: req-1\nCUSTOMER_NOT_ENABLED")
            return [{"customer.id": customer_id}]

        mock_search.side_effect = side_effect

        output = search.search_batch(
            customer_ids=["1111111111", "2222222222", "3333333333"],
            fields=["customer.id"],
            resource="customer",
        )

        self.assertEqual(
            sorted(output["results"]), ["1111111111", "3333333333"]
        )
        self.assertIn("CUSTOMER_NOT_ENABLED", output["errors"]["2222222222"])

    @patch("ads_mcp.tools.search.search")
    def test_search_batch_all_failed(self, mock_search):
        """Tests that the batch raises only when every customer fails."""
        mock_search.side_effect = ToolError("boom")

        with self.assertRaises(ToolError) as context:
            search.search_batch(
                customer_ids=["1111111111", "2222222222"],
                fields=["customer.id"],
                resource="customer",
            )
        self.assertIn(
            "Every customer in the batch failed", str(context.exception)
        )
        self.assertIn("1111111111", str(context.exception))

    def test_search_batch_empty_ids(self):
        """Tests that an empty customer id list is rejected."""
        with self.assertRaises(ToolError):
            search.search_batch(
                customer_ids=[], fields=["customer.id"], resource="customer"
            )

    @patch("ads_mcp.tools.search.search")
    def test_search_batch_too_many_ids(self, mock_search):
        """Tests that batches over the customer cap are rejected unqueried."""
        ids = [str(1000000000 + i) for i in range(51)]

        with self.assertRaises(ToolError) as context:
            search.search_batch(
                customer_ids=ids, fields=["customer.id"], resource="customer"
            )
        self.assertIn("at most 50", str(context.exception))
        mock_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
