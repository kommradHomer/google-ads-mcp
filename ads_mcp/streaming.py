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

"""Reads SearchStream results one row at a time.

The API streams results in messages of up to 10,000 rows. The client library
parses each message whole, and a parsed row costs several KiB however few
fields were selected, because every row carries the full layout of each
resource it touches: one message of 10,000 narrow rows takes about 60 MiB.
A stream keeps two or three of them alive at once, so a single large query
held around 200 MiB and an eight-account fan-out over 1 GiB, and the
container was killed for running out of memory.

`search_stream` asks gRPC for the raw bytes of each message instead and
parses one row at a time, so a stream holds one message's bytes (well under
1 MiB) and one parsed row.
"""

import sys
from types import SimpleNamespace
from typing import Any, Iterator, List, Tuple

import google.ads.googleads
from google.api_core import gapic_v1
from google.protobuf.field_mask_pb2 import FieldMask

_LENGTH_DELIMITED = 2
_SKIP_FIXED = {1: 8, 5: 4}  # wire type -> size of a fixed-width value

# The client-info header the Google Ads client library sends on its own calls.
_CLIENT_INFO = gapic_v1.client_info.ClientInfo(
    client_library_version=google.ads.googleads.VERSION
)


def _read_varint(data: bytes, pos: int) -> Tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("Truncated stream response.")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _split_response(
    data: bytes, results_field: int, mask_field: int
) -> Tuple[List[Tuple[int, int]], bytes]:
    """Locates the rows of a serialized stream response without parsing them.

    Returns the (start, end) offsets of each serialized row and the
    serialized field mask (empty when the message has none).
    """
    rows: List[Tuple[int, int]] = []
    mask = b""
    pos, end = 0, len(data)
    while pos < end:
        tag, pos = _read_varint(data, pos)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == _LENGTH_DELIMITED:
            size, pos = _read_varint(data, pos)
            if pos + size > end:
                raise ValueError("Truncated stream response.")
            if field == results_field:
                rows.append((pos, pos + size))
            elif field == mask_field:
                mask = data[pos : pos + size]
            pos += size
        elif wire_type == 0:
            _, pos = _read_varint(data, pos)
        elif wire_type in _SKIP_FIXED:
            pos += _SKIP_FIXED[wire_type]
        else:
            raise ValueError(
                f"Unexpected wire type {wire_type} in a stream response."
            )
    if pos != end:
        raise ValueError("Truncated stream response.")
    return rows, mask


class _Batch:
    """One stream message; mirrors the parsed response's `results` and
    `field_mask.paths`, with the rows parsed only as they are read."""

    def __init__(self, data: bytes, row_type: Any, fields: Tuple[int, int]):
        self._data = data
        self._row_type = row_type
        self._rows, mask = _split_response(data, *fields)
        self.field_mask = SimpleNamespace(
            paths=list(FieldMask.FromString(mask).paths)
        )

    @property
    def results(self) -> Iterator[Any]:
        data, parse = self._data, self._row_type.deserialize
        for start, end in self._rows:
            yield parse(data[start:end])


def search_stream(
    ga_service: Any, customer_id: str, query: str
) -> Iterator[_Batch]:
    """Runs `query` like `ga_service.search_stream`, parsing rows lazily.

    Yields one batch per stream message, each with `results` (the rows, as
    the same proto-plus `GoogleAdsRow` objects the client library returns)
    and `field_mask.paths`. Goes through the service's own channel, so
    credentials, request headers, and the conversion of API failures into
    `GoogleAdsException` are unchanged. Stopping early cancels the call.
    """
    # The request and row types of the API version this service was built
    # for, which is not necessarily the version imported elsewhere.
    types = sys.modules[type(ga_service).__module__].google_ads_service
    request_type = types.SearchGoogleAdsStreamRequest
    response_fields = types.SearchGoogleAdsStreamResponse.pb().DESCRIPTOR
    fields = (
        response_fields.fields_by_name["results"].number,
        response_fields.fields_by_name["field_mask"].number,
    )
    package = request_type.pb().DESCRIPTOR.file.package

    # The same call the generated client makes, minus response parsing:
    # no default timeout, the client-info header, and the routing header.
    rpc = gapic_v1.method.wrap_method(
        ga_service.transport.grpc_channel.unary_stream(
            f"/{package}.GoogleAdsService/SearchStream",
            request_serializer=request_type.serialize,
            response_deserializer=None,
        ),
        default_timeout=None,
        client_info=_CLIENT_INFO,
    )
    call = None
    finished = False
    try:
        call = rpc(
            request_type(customer_id=customer_id, query=query),
            metadata=(
                gapic_v1.routing_header.to_grpc_metadata(
                    (("customer_id", customer_id),)
                ),
            ),
        )
        for data in call:
            yield _Batch(data, types.GoogleAdsRow, fields)
        finished = True
    finally:
        if call is not None and not finished:
            call.cancel()
