# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test cases for the row-at-a-time SearchStream reader."""

import importlib
import sys
import unittest
from unittest.mock import MagicMock

from google.ads.googleads import client as googleads_client

from ads_mcp import streaming

# The service class of the API version the Google Ads client uses by
# default, and the message types that go with it.
_SERVICE_CLASS = importlib.import_module(
    f"google.ads.googleads.{googleads_client._DEFAULT_VERSION}"
    ".services.services.google_ads_service"
).GoogleAdsServiceClient
_TYPES = sys.modules[_SERVICE_CLASS.__module__].google_ads_service


def _response(first, count):
    """A stream response of `count` rows starting at row number `first`."""
    response = _TYPES.SearchGoogleAdsStreamResponse()
    for i in range(first, first + count):
        row = _TYPES.GoogleAdsRow()
        row.campaign.id = 1000 + i
        row.campaign.name = f"Campaign {i} – ünïcode"
        row.segments.date = "2026-11-%02d" % (1 + i % 28)
        row.metrics.cost_micros = 123456789 * (i + 1)
        row.metrics.conversions = i / 3
        response.results.append(row)
    response.field_mask.paths.extend(
        ["campaign.id", "campaign.name", "metrics.cost_micros"]
    )
    # Fields after the rows that the reader has to step over.
    response.summary_row.metrics.clicks = 42
    response.request_id = "request-" + "x" * 300
    response.query_resource_consumption = 2**40
    return response


class _FakeStub:
    """Plays the gRPC stub: returns the serialized messages of one call."""

    def __init__(self, messages):
        self.call = MagicMock()
        self.call.__iter__.return_value = iter(messages)
        self.calls = []

    def __call__(self, request, timeout=None, metadata=None, **kwargs):
        self.calls.append((request, timeout, dict(metadata)))
        return self.call


def _service(messages):
    """A service of the real client class whose channel serves `messages`."""
    service = _SERVICE_CLASS.__new__(_SERVICE_CLASS)  # no real channel
    service._transport = MagicMock()
    stub = _FakeStub(messages)
    service.transport.grpc_channel.unary_stream.return_value = stub
    return service, stub


class TestSearchStream(unittest.TestCase):
    def test_rows_equal_a_full_parse_of_the_same_messages(self):
        """Tests that lazily parsed rows match the client library's parse."""
        responses = [_response(0, 250), _response(250, 3), _response(253, 0)]
        messages = [
            _TYPES.SearchGoogleAdsStreamResponse.serialize(r) for r in responses
        ]
        service, _ = _service(messages)

        batches = list(streaming.search_stream(service, "123", "SELECT x"))

        self.assertEqual(len(batches), 3)
        for batch, message in zip(batches, messages):
            expected = _TYPES.SearchGoogleAdsStreamResponse.deserialize(message)
            rows = list(batch.results)
            self.assertEqual(len(rows), len(expected.results))
            for row, expected_row in zip(rows, expected.results):
                self.assertIsInstance(row, _TYPES.GoogleAdsRow)
                self.assertEqual(row, expected_row)
            self.assertEqual(
                batch.field_mask.paths, list(expected.field_mask.paths)
            )

    def test_a_message_without_rows_or_mask_is_an_empty_batch(self):
        """Tests that an empty stream message yields no rows."""
        service, _ = _service([b""])

        (batch,) = streaming.search_stream(service, "123", "SELECT x")

        self.assertEqual(list(batch.results), [])
        self.assertEqual(batch.field_mask.paths, [])

    def test_calls_search_stream_like_the_client_library(self):
        """Tests the method, request and headers of the underlying call."""
        service, stub = _service([])

        list(streaming.search_stream(service, "123", "SELECT campaign.id"))

        channel = service.transport.grpc_channel
        path = channel.unary_stream.call_args.args[0]
        self.assertEqual(
            path,
            f"/google.ads.googleads.{googleads_client._DEFAULT_VERSION}"
            ".services.GoogleAdsService/SearchStream",
        )
        # Raw bytes come back: parsing whole messages is what this avoids.
        self.assertIsNone(
            channel.unary_stream.call_args.kwargs["response_deserializer"]
        )
        ((request, timeout, metadata),) = stub.calls
        self.assertEqual(request.customer_id, "123")
        self.assertEqual(request.query, "SELECT campaign.id")
        self.assertIsNone(timeout)
        self.assertEqual(metadata["x-goog-request-params"], "customer_id=123")
        self.assertIn("gccl/", metadata["x-goog-api-client"])
        stub.call.cancel.assert_not_called()

    def test_closing_early_cancels_the_call(self):
        """Tests that a reader that stops mid-stream cancels the call."""
        message = _TYPES.SearchGoogleAdsStreamResponse.serialize(
            _response(0, 2)
        )
        service, stub = _service([message, message])

        stream = streaming.search_stream(service, "123", "SELECT x")
        next(stream)
        stream.close()

        stub.call.cancel.assert_called_once_with()

    def test_rejects_a_truncated_message(self):
        """Tests that a cut-off message fails instead of dropping rows."""
        message = _TYPES.SearchGoogleAdsStreamResponse.serialize(
            _response(0, 2)
        )
        service, _ = _service([message[:-5]])

        with self.assertRaises(ValueError):
            list(streaming.search_stream(service, "123", "SELECT x"))


if __name__ == "__main__":
    unittest.main()
