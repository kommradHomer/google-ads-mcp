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

"""Tools for exposing simple, core API methods to the MCP server."""

from typing import Any, Dict, List
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ads_mcp import customer_resolver

customers_mcp = FastMCP("customers")


@customers_mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def list_accessible_customers(
    include_inactive: bool = False,
) -> List[Dict[str, Any]]:
    """Returns the Google Ads accounts the authenticating user can work in.

    Use this tool first to discover available customer IDs if the user hasn't
    provided one. Most other tools require a valid customer ID as input.

    Access granted on a manager account (MCC) cascades to every account beneath
    it, so this expands each manager the user was granted and returns the
    accounts underneath, not just the manager itself.

    Each entry describes one account: `customer_id` to pass to other tools,
    `descriptive_name` as shown in the Google Ads UI, `manager`, `level` (0 is
    an account granted directly to the user), `status` (ENABLED, CANCELED,
    SUSPENDED or CLOSED), `currency_code` for reading cost metrics, and
    `time_zone`, which the account's dates are reported in.

    Read performance data only from entries where `manager` is False. A manager
    account (MCC) groups other accounts and runs no ads of its own, so asking
    one for `campaign` returns zero rows and asking it for metrics fails
    outright. Treat an empty result from a manager as "look in the accounts
    beneath it", never as "this business is not advertising". To see which
    accounts a manager groups, query `customer_client` on it.

    Args:
        include_inactive: Also return accounts that are not enabled (for
            example cancelled or suspended ones). Defaults to False.
    """
    access_map = customer_resolver.get_access_map()

    accounts = sorted(
        access_map.accounts.values(),
        key=lambda account: (account.level, account.descriptive_name),
    )

    return [
        {
            "customer_id": account.customer_id,
            "descriptive_name": account.descriptive_name,
            "manager": account.manager,
            "level": account.level,
            "status": account.status,
            "currency_code": account.currency_code,
            "time_zone": account.time_zone,
        }
        for account in accounts
        if include_inactive or account.status == "ENABLED"
    ]
