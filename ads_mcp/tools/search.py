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

"""Tools for exposing the API Search method to the MCP server."""

import contextvars
import textwrap
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List
from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

search_mcp = FastMCP("search")

import ads_mcp.utils as utils
from google.ads.googleads.errors import GoogleAdsException
from fastmcp.exceptions import ToolError

# Next-step guidance keyed by the ErrorCode oneof that Google set, so the
# model can correct a failed call in one retry instead of guessing.
_ERROR_HINTS = {
    "query_error": (
        "The query is invalid for this resource. Call "
        "get_resource_metadata(resource) to see its valid fields, metrics "
        "and segments, then retry once with corrected fields — do not "
        "guess variations."
    ),
    "authorization_error": (
        "This is an access problem, not a query problem. If the error is "
        "CUSTOMER_NOT_ENABLED the account is cancelled or suspended: skip "
        "this customer id instead of retrying."
    ),
    "quota_error": (
        "The API quota is exhausted. Wait before retrying; an immediate "
        "retry will fail again."
    ),
}


def _describe_error(error) -> str:
    """Renders one GoogleAdsError with everything Google told us.

    Adds the error-code class, the offending field path and a next-step
    hint to the plain message, each part best-effort: anything that cannot
    be extracted is simply omitted rather than masking the original error.
    """
    parts = []
    which = None
    try:
        which = error.error_code._pb.WhichOneof("error_code")
        if which:
            code = getattr(error.error_code, which)
            parts.append(f"[{which}.{code.name}]")
    except Exception:  # Never let diagnostics break error reporting.
        which = None
    try:
        path = ".".join(
            element.field_name
            for element in error.location.field_path_elements
            if element.field_name
        )
        if path:
            parts.append(f"at '{path}'")
    except Exception:
        pass
    parts.append(error.message)
    described = "Google Ads API Error " + " ".join(str(p) for p in parts)
    hint = _ERROR_HINTS.get(which)
    if hint:
        described += f"\nHint: {hint}"
    return described


def search(
    customer_id: str,
    fields: List[str],
    resource: str,
    conditions: List[str] = [],
    orderings: List[str] = [],
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    """Fetches data from the Google Ads API using the search method

    Args:
        customer_id: The id of the customer
        fields: The fields to fetch
        resource: The resource to return fields from
        conditions: List of conditions to filter the data, combined using AND clauses
        orderings: How the data is ordered
        limit: The maximum number of rows to return

    """

    ga_service = utils.get_googleads_service(
        "GoogleAdsService", customer_id=customer_id
    )

    query_parts = [f"SELECT {','.join(fields)} FROM {resource}"]

    if conditions:
        query_parts.append(f" WHERE {' AND '.join(conditions)}")

    if orderings:
        query_parts.append(f" ORDER BY {','.join(orderings)}")

    if limit:
        query_parts.append(f" LIMIT {limit}")

    query_parts.append(" PARAMETERS omit_unselected_resource_names=true")

    query = "".join(query_parts)
    utils.logger.info(f"ads_mcp.search query {query}")

    try:
        query_result = ga_service.search_stream(
            customer_id=customer_id, query=query
        )

        final_output: List = []
        for batch in query_result:
            for row in batch.results:
                final_output.append(
                    utils.format_output_row(row, batch.field_mask.paths)
                )
        return final_output
    except GoogleAdsException as ex:
        error_msgs = [_describe_error(error) for error in ex.failure.errors]
        utils.logger.warning(
            "ads_mcp.search failed: "
            + " | ".join(msg.splitlines()[0] for msg in error_msgs)
        )
        raise ToolError(
            f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
        )


_SEARCH_BATCH_MAX_CUSTOMERS = 50
_SEARCH_BATCH_MAX_WORKERS = 8


def search_batch(
    customer_ids: List[str],
    fields: List[str],
    resource: str,
    conditions: List[str] = [],
    orderings: List[str] = [],
    limit: int | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Runs one search query against many customers in a single call.

    Use this instead of looping `search` per customer whenever the same
    query should be answered for several customer ids (for example, the
    spend of every account under a manager). One call here replaces one
    `search` call per customer. Query syntax, hints, and the list of valid
    resources are documented on the `search` tool; the query arguments are
    identical, only `customer_ids` differs.

    Accepts at most 50 customer ids per call; chunk larger sets. `limit`
    applies per customer, not to the batch as a whole.

    Returns an object with two keys: `results` maps each customer id to its
    rows, and `errors` maps each customer id that failed to its error
    message. A failing customer never fails the batch; only if every
    customer fails is an error raised.

    Args:
        customer_ids: The ids of the customers to run the query against
        fields: The fields to fetch
        resource: The resource to return fields from
        conditions: List of conditions to filter the data, combined using AND clauses
        orderings: How the data is ordered
        limit: The maximum number of rows to return per customer

    """
    if not customer_ids:
        raise ToolError("customer_ids must not be empty.")

    # Dedupe while preserving order so one account is only queried once.
    customer_ids = list(dict.fromkeys(customer_ids))

    if len(customer_ids) > _SEARCH_BATCH_MAX_CUSTOMERS:
        raise ToolError(
            f"Too many customer ids ({len(customer_ids)}); at most "
            f"{_SEARCH_BATCH_MAX_CUSTOMERS} per call. Split the ids into "
            "chunks and call this tool once per chunk."
        )

    utils.logger.info(
        f"ads_mcp.search_batch fan-out over {len(customer_ids)} customer(s)"
    )

    def run_one(customer_id: str) -> List[Dict[str, Any]]:
        return search(
            customer_id=customer_id,
            fields=fields,
            resource=resource,
            conditions=conditions,
            orderings=orderings,
            limit=limit,
        )

    # The FastMCP access token lives in a contextvar, which does not
    # propagate into worker threads by itself; each task runs in its own
    # copy of the calling context so per-user credentials keep working.
    workers = min(_SEARCH_BATCH_MAX_WORKERS, len(customer_ids))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            customer_id: executor.submit(
                contextvars.copy_context().run, run_one, customer_id
            )
            for customer_id in customer_ids
        }

    results: Dict[str, List[Dict[str, Any]]] = {}
    errors: Dict[str, str] = {}
    for customer_id, future in futures.items():
        try:
            results[customer_id] = future.result()
        except Exception as ex:
            errors[customer_id] = str(ex)

    if not results:
        raise ToolError(
            "Every customer in the batch failed:\n"
            + "\n".join(f"{cid}: {msg}" for cid, msg in errors.items())
        )

    return {"results": results, "errors": errors}


def _dedent_docstring(docstring: str) -> str:
    """Removes the body indentation of a docstring, keeping its summary line.

    Only affects how the docstring reads once embedded in the generated
    description below, which mixes it with unindented sections. The tool's
    argument descriptions are parsed from the pristine docstring instead, so
    this no longer has to preserve anything a docstring parser relies on.
    """
    summary, separator, body = docstring.partition("\n")
    return summary + separator + textwrap.dedent(body)


def _search_tool_description() -> str:
    """Returns the description for the `search` tool."""
    # Add a warning that will be part of the description
    file_content = (
        "WARNING: The list of valid resources is missing. "
        "Tool may not function correctly."
    )

    try:
        with open(utils.get_gaql_resources_filepath(), "r") as file:
            file_content = file.read()
    except FileNotFoundError:
        utils.logger.error("The specified file was not found.")

    return f"""
{_dedent_docstring(search.__doc__)}

### Hints
    Language Grammar can be found at https://developers.google.com/google-ads/api/docs/query/grammar
    All resources and descriptions are found at https://developers.google.com/google-ads/api/fields/latest/overview
    If the query fails, a ToolError will be raised with the error details.

    For Conversion issues try looking in offline_conversion_upload_conversion_action_summary

### Hint for customer_id
    should be a string of numbers without punctuation
    if presented in the form 123-456-7890 remove the hyphens and use 1234567890

### Hints for Dates
    All dates should be in the form YYYY-MM-DD and must include the dashes (-)
    Date ranges must be finite and must include a start and end date

### Hints for limits
    Requests to resource change_event must specify a LIMIT of less than or equal to 10000

### Hints for conversions questions
    https://developers.google.com/google-ads/api/docs/conversions/upload-summaries 


### Hints for all resources
    To find out which specific fields (including compatible metrics and segments) you can select, filter by, or sort by for a given resource, you MUST use the `get_resource_metadata` tool.
    Do not guess the fields. Use the tool to look them up.
    Once you have the fields, ensure the whole field name is used (e.g., 'campaign.id', not just 'id'). Wildcards and partial fields are not allowed.

### Valid resources
    What follows is a list of valid resources that can be queried.
    {file_content}
"""


# The `search` tool requires a more complex description that's generated at
# runtime. Uses the `add_tool` method instead of an annnotation since `add_tool`
# provides the flexibility needed to generate the description while also
# including the `search` method's docstring.
#
# The description is passed explicitly rather than written onto `search.__doc__`.
# Assigning it to the docstring makes FastMCP's docstring parser treat the whole
# generated text as one docstring and keep only its summary line, silently
# dropping the hints and the list of valid resources. Leaving the docstring
# pristine keeps the `Args:` block parsable into the argument schema, and the
# full text still reaches the model as the description.
search_mcp.add_tool(
    Tool.from_function(
        search,
        description=_search_tool_description(),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
)

# Registered the same way as `search` so the module-level function stays
# directly callable (the batch tool fans out onto it, and tests exercise it).
# Its description is its docstring: deliberately short, deferring syntax and
# the resource list to `search` so the tool list doesn't carry that text twice.
search_mcp.add_tool(
    Tool.from_function(
        search_batch,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
)
