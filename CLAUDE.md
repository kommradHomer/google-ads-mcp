# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

An [MCP](https://modelcontextprotocol.io) server (`ads_mcp` package) that exposes the
[Google Ads API](https://developers.google.com/google-ads/api) as MCP Tools and Resources.
Built on [FastMCP](https://gofastmcp.com) and the `google-ads` Python client library.
Distributed for `pipx run` from git; also deployable to Cloud Run as a web service.

## Commands

Dev setup: `pip install -e .[dev]` (installs `nox` and `black`).

- `nox -s format` — apply `black` formatting (80-char line width, PEP 8). CI runs `nox -s lint` which fails on unformatted code.
- `nox -s tests` — run unit tests across all installed Python versions (3.10–3.13). Target one version with `nox -s tests-3.13`.
- Run a single test file/case directly: `python -m unittest tests.tools.search_test` or `python -m unittest tests.tools.search_test.ClassName.test_method`.
- `nox -s smoke_tests` — assert the tools/resources lists match the golden files in `tests/smoke/`.
- `nox -s update_smoke_golden` — regenerate those golden files after intentional tool/resource changes.
- `nox -s llm_tests` — LLM tool-selection tests (needs `google-genai` and `GEMINI_API_KEY`).

Run the server locally: `google-ads-mcp` (entry point → `ads_mcp.server:run_server`).

Tests use `unittest` (files matched by `*_test.py`) with `pyfakefs` for filesystem mocking.

## Architecture

**Singleton MCP + dynamic mounting.** `ads_mcp/coordinator.py` owns the single `FastMCP` instance
(`mcp`). At import time it runs `initialize_and_mount_tools()`, which:
1. Reflectively scans every module under `ads_mcp/tools/` for module-level `FastMCP` sub-server
   instances (e.g. `search_mcp`, `customers_mcp`, `metadata_mcp`). Each sub-server's `.name` is its
   **category**.
2. Loads `ToolsConfig` (see below), skips disabled categories, removes disabled tools from the
   sub-server, then `mount`s it under the configured namespace prefix.

Because tools are discovered by reflection, **a new tool = a new module in `ads_mcp/tools/` that
defines a `FastMCP` sub-server and registers tools on it** (via `@sub_mcp.tool(...)` or
`sub_mcp.add_tool(...)`). No manual wiring in the coordinator. Resources, by contrast, register
directly on the shared `mcp` via `@mcp.resource(...)` and must be imported in `ads_mcp/server.py`
for their decorators to run (that's why the resource imports there are marked `# noqa: F401`).

**Tool categories** (`ALL_CATEGORIES` in `config.py`): `customers`, `search`, `metadata`. These names
must match the sub-server `.name`s.

**Configuration** (`ads_mcp/config.py`, `ToolsConfig`): enables/disables tool categories (namespaces)
and individual tools, and sets namespace prefixes, via `tools_config.yaml`. Resolution order:
explicit path → `GOOGLE_ADS_MCP_TOOLS_CONFIG` env var → `tools_config.yaml` in cwd → the bundled
default (`ads_mcp/tools_config.yaml`, shipped via `MANIFEST.in` so `pipx` installs work out of the
box). An explicitly requested-but-missing or malformed file raises rather than falling back silently.

**Auth / transport** (`coordinator.py` + `server.py`): if `GOOGLE_ADS_MCP_OAUTH_CLIENT_ID` and
`GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET` are set, the server uses FastMCP's `GoogleProvider` OAuth proxy
and runs over `streamable-http` (for web/Cloud Run deployment). Otherwise it uses Application Default
Credentials and runs over `stdio` (local MCP client use).

**API client** (`ads_mcp/utils.py`): `get_googleads_service()` builds a `GoogleAdsClient` per call.
Credentials come from the FastMCP OAuth access token if present, else ADC. Every service call goes
through `MCPHeaderInterceptor` (`mcp_header_interceptor.py`) which adds a usage-tracking header.
`format_output_row`/`format_output_value` convert proto responses (enums → names, messages → dicts)
into JSON-serializable output. `GOOGLE_ADS_DEVELOPER_TOKEN` is required; `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
is optional (needed for manager-account access).

**GAQL resource list.** The `search` tool's description is generated at runtime and embeds the
contents of `ads_mcp/gaql_resources.txt` (the list of valid queryable resources). Regenerate that
file against the live API with the `google-ads-mcp-update-gaql` entry point
(`ads_mcp/update_references.py`).

## Gotchas

- **Google Ads API version is hardcoded as `v24`** in multiple places (`utils.py` imports from
  `google.ads.googleads.v24...`, `get_resource_metadata.py`, `resources/discovery.py`'s discovery URL).
  Bumping the API version means updating all of these.
- After changing any tool or resource surface (names, descriptions, args), the smoke tests will fail
  until you run `nox -s update_smoke_golden` and commit the updated golden JSON.
- Contributions require a signed Google CLA (see `CONTRIBUTING.md`).
