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

"""A stand-in for `ads_mcp.streaming.search_stream` in tool tests."""


def search_stream(ga_service, customer_id, query):
    """Yields the batches of a fake service's `search_stream`.

    Tool tests fake the service's `search_stream`; the real reader talks to
    the service's gRPC channel and is covered by `streaming_test`. Like the
    real reader, this cancels the stream when it is closed early.
    """
    stream = ga_service.search_stream(customer_id=customer_id, query=query)
    finished = False
    try:
        yield from stream
        finished = True
    finally:
        if not finished and hasattr(stream, "cancel"):
            stream.cancel()
