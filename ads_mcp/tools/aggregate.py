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

"""The aggregate tool: summarises rows on the server instead of returning them.

GAQL has no GROUP BY, COUNT or SUM across rows, so questions such as "which
campaigns carry this label" or "which search terms spent money without
converting" used to be answered by fetching every row. In large accounts that
meant tens of thousands of rows per call: more than the model can read, and
enough to exhaust the container's memory. This tool streams the rows and keeps
only one running total per group, so its memory grows with the number of
groups, never with the number of rows.
"""

import contextvars
import json
import operator
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Tuple

from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.tools import search as search_tools

# Registered on the search sub-server so it mounts as `search_aggregate` next
# to `search_search` and `search_search_batch`. The coordinator keys
# sub-servers by name, so importing the instance here does not mount it twice.
from ads_mcp.tools.search import search_mcp

ROW_COUNT = "row_count"
CURRENCY_FIELD = "customer.currency_code"

_DEFAULT_TOP = 100
_MAX_TOP = 1000

# Upper bound on distinct groups held in memory across one whole call, so a
# group_by on a near-unique field cannot rebuild the row pile this tool
# exists to avoid. 100k small groups stay in the tens of MiB.
_MAX_GROUPS = 100_000

# Substrings that mark a metric as a ratio, average, share, score or date:
# adding such values up across rows produces a meaningless number.
_NON_ADDITIVE_MARKERS = (
    "ctr",
    "average",
    "rate",
    "share",
    "percent",
    "per_",
    "quality",
    "predicted",
    "frequency",
    "unique_users",
    "last_",
    "score",
    "benchmark",
    "margin",
    "measurability",
    "viewability",
    "cpm",
    "cpc",
    "cpv",
    "cpe",
)

# Metrics denominated in the account's currency. Summing them across accounts
# is only meaningful within one currency.
_MONEY_MARKERS = ("micros", "cost", "value")

# Ratios recomputed from summed parts: name -> (numerator, denominator, scale,
# rounded to whole micros). Averaging per-row ratios would weight a row with
# 1 click the same as a row with 10,000.
_DERIVED = {
    "derived.ctr": ("metrics.clicks", "metrics.impressions", 1, False),
    "derived.average_cpc_micros": (
        "metrics.cost_micros",
        "metrics.clicks",
        1,
        True,
    ),
    "derived.average_cpm_micros": (
        "metrics.cost_micros",
        "metrics.impressions",
        1000,
        True,
    ),
    "derived.conversion_rate": (
        "metrics.conversions",
        "metrics.clicks",
        1,
        False,
    ),
    "derived.cost_per_conversion_micros": (
        "metrics.cost_micros",
        "metrics.conversions",
        1,
        True,
    ),
    "derived.value_per_conversion": (
        "metrics.conversions_value",
        "metrics.conversions",
        1,
        False,
    ),
    "derived.roas": (
        "metrics.conversions_value",
        "metrics.cost_micros",
        1_000_000,
        False,
    ),
}

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_ORDER_BY_RE = re.compile(r"^\s*([a-z][\w.]*)(?:\s+(asc|desc))?\s*$", re.I)
_HAVING_RE = re.compile(
    r"^\s*([a-z][\w.]*)\s*(<=|>=|!=|=|<|>)\s*(-?\d+(?:\.\d+)?)\s*$", re.I
)
_COMPARATORS = {
    "=": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}

# Currency never changes for an account, so it is looked up once per process.
_currency_cache: Dict[str, str] = {}


class _TooManyGroups(ToolError):
    """Raised when a call would hold more than `_MAX_GROUPS` groups."""


class _GroupBudget:
    """Counts groups created across every worker of one call."""

    def __init__(self, limit: int):
        self._left = limit
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self._left <= 0:
                raise _TooManyGroups(
                    f"More than {_MAX_GROUPS:,} distinct groups. Group by "
                    "fewer or coarser fields, or narrow `conditions` (for "
                    "example a shorter date range or fewer campaigns)."
                )
            self._left -= 1


def _is_money(metric: str) -> bool:
    return any(marker in metric for marker in _MONEY_MARKERS)


def _dedupe(items: List[str]) -> List[str]:
    return list(dict.fromkeys(item.strip() for item in items))


def _validate_fields(group_by: List[str], metrics: List[str]) -> None:
    for field in group_by + metrics:
        if not _FIELD_RE.match(field):
            raise ToolError(
                f"'{field}' is not a full field name. Use names such as "
                "'campaign.id' or 'metrics.clicks'."
            )
    for field in group_by:
        if field.startswith("metrics."):
            raise ToolError(
                f"'{field}' is a metric and cannot be grouped on. Put it in "
                "`metrics` to sum it, or in `having` to filter on its total."
            )
    for metric in metrics:
        if not metric.startswith("metrics."):
            raise ToolError(
                f"'{metric}' is not a metric. Only metrics.* fields can be "
                "summed; group on other fields with `group_by`."
            )
        name = metric[len("metrics.") :]
        if any(marker in name for marker in _NON_ADDITIVE_MARKERS):
            raise ToolError(
                f"'{metric}' is a ratio, average, share or score and cannot "
                "be added up across rows. Sum its parts instead (for example "
                "metrics.clicks and metrics.impressions for CTR, "
                "metrics.cost_micros and metrics.conversions for cost per "
                "conversion); the ratio is then recomputed from the totals "
                "as a derived.* column. For metrics that have no additive "
                "parts, such as impression share, use `search` at the level "
                "you need."
            )


def _derived_for(metrics: List[str]) -> List[str]:
    """Returns the derived ratios whose parts are all being summed."""
    return [
        name
        for name, (num, den, _, _) in _DERIVED.items()
        if num in metrics and den in metrics
    ]


def _parse_order_by(
    order_by: str | None,
    columns: List[str],
    metrics: List[str],
) -> Tuple[str, bool]:
    """Returns (column, descending) for sorting the groups."""
    if not order_by:
        return (metrics[0] if metrics else ROW_COUNT), True
    match = _ORDER_BY_RE.match(order_by)
    if not match or match.group(1) not in columns:
        raise ToolError(
            f"order_by '{order_by}' must be one output column optionally "
            f"followed by ASC or DESC. Columns: {', '.join(columns)}. A "
            "derived.* column exists only when both of its parts are in "
            "`metrics`."
        )
    direction = (match.group(2) or "DESC").upper()
    return match.group(1), direction == "DESC"


def _parse_having(
    having: List[str], numeric_columns: List[str], group_by: List[str]
) -> List[Callable[[Dict[str, Any]], bool]]:
    """Turns `having` strings into predicates over a finished group."""
    predicates = []
    for clause in having:
        match = _HAVING_RE.match(clause)
        if not match:
            raise ToolError(
                f"having '{clause}' must look like '<column> <op> <number>' "
                "with op one of =, !=, >, >=, <, <= (for example "
                "'metrics.conversions = 0')."
            )
        column, op, number = match.groups()
        if column in group_by or column == CURRENCY_FIELD:
            raise ToolError(
                f"having '{clause}' filters on a group_by field. Filter on "
                "fields in `conditions` instead; `having` is for totals."
            )
        if column not in numeric_columns:
            raise ToolError(
                f"having '{clause}' refers to '{column}', which is not an "
                f"output total. Columns: {', '.join(numeric_columns)}."
            )
        compare, value = _COMPARATORS[op], float(number)

        def predicate(group, column=column, compare=compare, value=value):
            actual = group.get(column)
            return actual is not None and compare(actual, value)

        predicates.append(predicate)
    return predicates


def _build_query(
    resource: str,
    group_by: List[str],
    metrics: List[str],
    conditions: List[str],
) -> str:
    # With nothing to select, select the row's own name so rows can be
    # counted; every resource has one.
    fields = group_by + metrics or [f"{resource}.resource_name"]
    query = f"SELECT {','.join(fields)} FROM {resource}"
    if conditions:
        query += f" WHERE {' AND '.join(conditions)}"
    return query + " PARAMETERS omit_unselected_resource_names=true"


def _raise_api_error(
    ex: GoogleAdsException, caller: str, customer_id: str
) -> None:
    error_msgs = [
        search_tools._describe_error(error) for error in ex.failure.errors
    ]
    utils.logger.warning(
        f"ads_mcp.aggregate failed [{caller} cid={customer_id}]: "
        + " | ".join(msg.splitlines()[0] for msg in error_msgs)
    )
    raise ToolError(f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs))


def _currency_of(customer_id: str) -> str:
    """Returns the account's currency code, cached for the process."""
    if customer_id in _currency_cache:
        return _currency_cache[customer_id]
    ga_service = utils.get_googleads_service(
        "GoogleAdsService", customer_id=customer_id
    )
    code = ""
    try:
        for batch in ga_service.search_stream(
            customer_id=customer_id,
            query="SELECT customer.currency_code FROM customer",
        ):
            for row in batch.results:
                code = row.customer.currency_code
    except GoogleAdsException as ex:
        _raise_api_error(ex, search_tools._caller_label(), customer_id)
    _currency_cache[customer_id] = code
    return code


def _hashable(value: Any) -> Any:
    """Makes repeated and message field values usable in a group key."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _scan_customer(
    customer_id: str,
    query: str,
    group_by: List[str],
    metrics: List[str],
    key_prefix: Tuple[Any, ...],
    budget: _GroupBudget,
) -> Tuple[Dict[Tuple[Any, ...], list], int]:
    """Streams one customer's rows into per-group running totals.

    Returns the groups, each `[group values, row count, *metric sums]`, and
    the number of rows scanned. Rows are discarded as soon as they are added.
    """
    ga_service = utils.get_googleads_service(
        "GoogleAdsService", customer_id=customer_id
    )
    caller = search_tools._caller_label()
    utils.logger.info(
        f"ads_mcp.aggregate query [{caller} cid={customer_id}] {query}"
    )
    started = time.monotonic()
    groups: Dict[Tuple[Any, ...], list] = {}
    rows = 0
    try:
        for batch in ga_service.search_stream(
            customer_id=customer_id, query=query
        ):
            for row in batch.results:
                rows += 1
                values = key_prefix + tuple(
                    utils.format_output_value(utils.get_nested_attr(row, f))
                    for f in group_by
                )
                key = tuple(_hashable(v) for v in values)
                totals = groups.get(key)
                if totals is None:
                    budget.take()
                    totals = groups[key] = [values, 0] + [0] * len(metrics)
                totals[1] += 1
                for i, metric in enumerate(metrics):
                    value = utils.format_output_value(
                        utils.get_nested_attr(row, metric)
                    )
                    if isinstance(value, bool) or not isinstance(
                        value, (int, float)
                    ):
                        raise ToolError(
                            f"'{metric}' returned {value!r}, which is not a "
                            "number and cannot be summed."
                        )
                    totals[2 + i] += value
    except GoogleAdsException as ex:
        _raise_api_error(ex, caller, customer_id)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    utils.logger.info(
        f"ads_mcp.aggregate done [{caller} cid={customer_id}] {rows} row(s) "
        f"into {len(groups)} group(s) in {elapsed_ms}ms"
    )
    return groups, rows


def _tidy(value: Any) -> Any:
    """Removes float noise from summed values."""
    return round(value, 6) if isinstance(value, float) else value


def _finish_group(
    key_fields: List[str],
    values: Tuple[Any, ...],
    count: int,
    sums: List[Any],
    metrics: List[str],
    derived: List[str],
) -> Dict[str, Any]:
    group: Dict[str, Any] = dict(zip(key_fields, values))
    group[ROW_COUNT] = count
    for metric, total in zip(metrics, sums):
        group[metric] = _tidy(total)
    for name in derived:
        numerator, denominator, scale, whole = _DERIVED[name]
        bottom = group[denominator]
        if not bottom:
            group[name] = None
            continue
        ratio = group[numerator] * scale / bottom
        group[name] = int(round(ratio)) if whole else round(ratio, 6)
    return group


def _sort_groups(
    groups: List[Dict[str, Any]], column: str, descending: bool
) -> List[Dict[str, Any]]:
    """Sorts on one column, groups without a value last, ties stable."""
    present = [g for g in groups if g.get(column) is not None]
    missing = [g for g in groups if g.get(column) is None]
    present.sort(key=lambda g: g[column], reverse=descending)
    return present + missing


def aggregate(
    customer_ids: List[str],
    resource: str,
    group_by: List[str] = [],
    metrics: List[str] = [],
    conditions: List[str] = [],
    having: List[str] = [],
    order_by: str | None = None,
    top: int = _DEFAULT_TOP,
) -> Dict[str, Any]:
    """Answers total, count, distinct-value and top-N questions over many rows.

    Prefer this over `search_search` and `search_search_batch` whenever the
    question is about totals or rankings rather than individual rows. For
    example: which campaigns carry a label; how many ads are disapproved;
    which search terms spent money without converting; the top 50 keywords
    by cost; total spend per channel across accounts. It scans every matching
    row on the server and returns only the groups, so it handles search
    terms, ads, keywords and daily rows in large accounts, where
    `search_search` would return tens of thousands of rows. Query syntax,
    hints, and the list of valid resources are documented on the
    `search_search` tool; `resource` and `conditions` work exactly as there.

    The rows of SELECT <group_by>, <metrics> FROM <resource> WHERE
    <conditions> are grouped by their `group_by` values. Each group reports
    `row_count` (the number of API rows in it, e.g. ads or search-term rows)
    and the sum of every field in `metrics`.
    - Distinct values: set `group_by`, leave `metrics` empty. E.g. campaigns
      carrying a label: resource ad_group_ad_label, group_by
      [campaign.id, campaign.name], conditions [label.id = 123].
    - Counts by value: e.g. group_by
      [ad_group_ad.policy_summary.approval_status] on ad_group_ad.
    - Grand total: leave `group_by` empty.
    - `metrics` takes additive metrics only (cost_micros, clicks,
      impressions, conversions, conversions_value, ...). Ratios are
      recomputed from the totals when both parts are summed: derived.ctr,
      derived.average_cpc_micros, derived.average_cpm_micros,
      derived.conversion_rate (conversions / clicks),
      derived.cost_per_conversion_micros, derived.value_per_conversion,
      derived.roas (conversions_value per unit of cost). Ratio, average and
      share metrics such as metrics.ctr or metrics.search_impression_share
      are rejected because they cannot be added up.
    - `conditions` filter single rows before grouping. `having` filters
      groups by their totals: ["metrics.conversions = 0",
      "metrics.cost_micros > 50000000"] keeps search terms that spent over
      50 in total without converting in any ad group. Put every condition on
      a total in `having`, not `conditions`.
    - Results are sorted by `order_by` (one output column, optional ASC or
      DESC; default the first metric, else row_count, descending) and capped
      at `top` groups. `groups_matched` says how many groups passed
      `having`; `totals` sums all of them, not just the returned ones.
    - Several customer ids (at most 50) are merged into the same groups.
      For one group per account instead, add customer.id (and
      customer.descriptive_name) to `group_by`. When money metrics are
      summed across accounts, groups are also split by
      customer.currency_code so currencies never mix. A customer that fails
      is listed in `errors`; an invalid query fails at once.

    Args:
        customer_ids: The ids of the customers to aggregate over
        resource: The resource to read rows from
        group_by: Fields whose values define the groups
        metrics: Additive metrics to sum per group
        conditions: Row filters applied before grouping, combined using AND
        having: Filters on group totals, like 'metrics.conversions = 0'
        order_by: Output column to sort groups by, optional ASC or DESC
        top: The maximum number of groups to return (1-1000)

    """
    if not customer_ids:
        raise ToolError("customer_ids must not be empty.")
    customer_ids = list(dict.fromkeys(customer_ids))
    if len(customer_ids) > search_tools._SEARCH_BATCH_MAX_CUSTOMERS:
        raise ToolError(
            f"Too many customer ids ({len(customer_ids)}); at most "
            f"{search_tools._SEARCH_BATCH_MAX_CUSTOMERS} per call. Split the "
            "ids into chunks and call this tool once per chunk."
        )
    if not 1 <= top <= _MAX_TOP:
        raise ToolError(f"top must be between 1 and {_MAX_TOP}.")

    group_by, metrics = _dedupe(group_by), _dedupe(metrics)
    _validate_fields(group_by, metrics)
    derived = _derived_for(metrics)
    split_by_currency = len(customer_ids) > 1 and any(
        _is_money(m) for m in metrics
    )
    key_fields = ([CURRENCY_FIELD] if split_by_currency else []) + group_by
    numeric_columns = [ROW_COUNT] + metrics + derived
    sort_column, descending = _parse_order_by(
        order_by, key_fields + numeric_columns, metrics
    )
    predicates = _parse_having(having, numeric_columns, group_by)
    query = _build_query(resource, group_by, metrics, conditions)

    utils.logger.info(
        f"ads_mcp.aggregate [{search_tools._caller_label()}] over "
        f"{len(customer_ids)} customer(s)"
    )
    budget = _GroupBudget(_MAX_GROUPS)

    def run_one(customer_id: str):
        prefix = (_currency_of(customer_id),) if split_by_currency else ()
        return _scan_customer(
            customer_id, query, group_by, metrics, prefix, budget
        )

    partials: List[Tuple[Dict[Tuple[Any, ...], list], int]] = []
    errors: Dict[str, str] = {}

    # Validate the query on one customer before fanning out, exactly like
    # search_batch: a structurally invalid query fails the same everywhere.
    canary_id, remaining_ids = customer_ids[0], customer_ids[1:]
    try:
        partials.append(run_one(canary_id))
    except _TooManyGroups:
        raise
    except Exception as ex:
        if remaining_ids and search_tools._is_query_structure_error(str(ex)):
            raise ToolError(
                f"{ex}\nThe query was not run against the other "
                f"{len(remaining_ids)} customer(s): it is invalid "
                "regardless of customer. Fix the query and retry."
            )
        if not remaining_ids:
            raise
        errors[canary_id] = str(ex)

    # Each worker runs in a copy of the calling context so the per-user
    # OAuth token (a contextvar) reaches it.
    if remaining_ids:
        workers = min(
            search_tools._SEARCH_BATCH_MAX_WORKERS, len(remaining_ids)
        )
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                customer_id: executor.submit(
                    contextvars.copy_context().run, run_one, customer_id
                )
                for customer_id in remaining_ids
            }
        for customer_id, future in futures.items():
            try:
                partials.append(future.result())
            except _TooManyGroups:
                raise
            except Exception as ex:
                errors[customer_id] = str(ex)

    if not partials:
        raise ToolError(
            "Every customer failed:\n"
            + "\n".join(f"{cid}: {msg}" for cid, msg in errors.items())
        )

    merged: Dict[Tuple[Any, ...], list] = {}
    rows_scanned = 0
    for groups, rows in partials:
        rows_scanned += rows
        for key, totals in groups.items():
            into = merged.get(key)
            if into is None:
                merged[key] = totals
            else:
                for i in range(1, len(totals)):
                    into[i] += totals[i]

    finished = [
        _finish_group(key_fields, values, count, sums, metrics, derived)
        for values, count, *sums in merged.values()
    ]
    matched = [g for g in finished if all(p(g) for p in predicates)]

    # Totals over every matched group, one line per currency when split.
    by_currency: Dict[Any, list] = {}
    for g in matched:
        currency = g.get(CURRENCY_FIELD) if split_by_currency else None
        line = by_currency.setdefault(currency, [0] + [0] * len(metrics))
        line[0] += g[ROW_COUNT]
        for i, metric in enumerate(metrics):
            line[1 + i] += g[metric]
    totals = [
        _finish_group(
            [CURRENCY_FIELD] if split_by_currency else [],
            (currency,) if split_by_currency else (),
            line[0],
            line[1:],
            metrics,
            derived,
        )
        for currency, line in by_currency.items()
    ]

    returned = _sort_groups(matched, sort_column, descending)[:top]
    return {
        "query": query,
        "rows_scanned": rows_scanned,
        "groups_found": len(finished),
        "groups_matched": len(matched),
        "groups_returned": len(returned),
        "groups": returned,
        "totals": totals,
        "errors": errors,
    }


# Registered with `add_tool` like the search tools, so the module-level
# function stays directly callable from tests.
search_mcp.add_tool(
    Tool.from_function(
        aggregate,
        annotations=ToolAnnotations(readOnlyHint=True),
    )
)
