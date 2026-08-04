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

"""Names the Google account behind each sign-in and session refresh.

The OAuth handshake happens entirely inside FastMCP's proxy, which logs only an
opaque client id. That is not enough to answer the question support actually
gets -- "I had to log in five times today, why?" -- because it says neither who
the user was nor whether they really re-consented rather than refreshing
silently in the background.

Both facts are available at the moment the proxy trades upstream tokens, so
this module subclasses the provider to log them:

  ads_mcp auth: roberto@example.com SIGNED IN (completed the consent screen)
  ads_mcp auth: roberto@example.com refreshed silently (no sign-in needed)

A sign-in line means the person saw a Google consent screen; a refresh line
means they did not.

Refreshes reach us by two different routes, and both have to be covered or the
log is misleading. The client calls the token endpoint roughly once a day
(`exchange_refresh_token`), because the FastMCP token it holds lasts 24h -- but
the Google token underneath expires hourly, and those refreshes are done out of
band by the proxy itself (`_try_transparent_refresh`). Covering only the first
would leave ~23 of every 24 refreshes unlogged, making a healthy session look
dormant.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import json
import logging
from typing import Any, Optional

from fastmcp.server.auth.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

_UNKNOWN = "unknown user"

# Carries the email from `_extract_upstream_claims` (which is handed the
# upstream token response) up to the exchange method that logs the event. A
# ContextVar rather than an attribute, so two people signing in at once cannot
# be attributed to each other.
_upstream_email: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ads_mcp_upstream_email", default=None
)


def _email_from_id_token(id_token: str) -> Optional[str]:
    """Returns the `email` claim of an OIDC id token, without verifying it.

    Signature verification is deliberately skipped. The token arrived directly
    from Google's token endpoint over TLS, and the value is only ever written
    to a log line -- it grants nothing -- so there is no untrusted party here
    to defend against.
    """
    try:
        payload = id_token.split(".")[1]
        # JWT segments are base64url without padding; restore it.
        padding = "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (
        IndexError,
        ValueError,
        binascii.Error,
        UnicodeDecodeError,
    ):
        return None

    email = claims.get("email") if isinstance(claims, dict) else None
    return email if isinstance(email, str) and email else None


def _email_from_token_set(token_set: Any) -> Optional[str]:
    """Returns the email recorded in a stored upstream token set.

    `raw_token_data` keeps the provider's last token response, and the proxy
    merges rather than replaces it on refresh, so the id token from the
    original sign-in is still there even when a refresh response omits one.
    """
    raw = getattr(token_set, "raw_token_data", None)
    if not isinstance(raw, dict):
        return None
    id_token = raw.get("id_token")
    return _email_from_id_token(id_token) if isinstance(id_token, str) else None


class LoggingGoogleProvider(GoogleProvider):
    """A `GoogleProvider` that names the user in its sign-in/refresh logs.

    Every override delegates to `super()` and only adds a log line, so a
    failure to identify the user degrades to `unknown user` and never blocks
    anyone from signing in.
    """

    async def _extract_upstream_claims(
        self, idp_tokens: dict[str, Any]
    ) -> dict[str, Any] | None:
        # Called by the proxy on both the sign-in and the refresh path, with
        # the raw token response from Google. Google includes an id token
        # whenever the `openid` scope was granted, which it is here.
        try:
            id_token = (idp_tokens or {}).get("id_token")
            if isinstance(id_token, str):
                email = _email_from_id_token(id_token)
                if email:
                    _upstream_email.set(email)
        except Exception:  # never let logging break the token exchange
            logger.debug("ads_mcp auth: could not read email", exc_info=True)

        return await super()._extract_upstream_claims(idp_tokens)

    async def exchange_authorization_code(self, client, authorization_code):
        _upstream_email.set(None)
        token = await super().exchange_authorization_code(
            client, authorization_code
        )
        logger.info(
            "ads_mcp auth: %s SIGNED IN (completed the consent screen)",
            _upstream_email.get() or _UNKNOWN,
        )
        return token

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        _upstream_email.set(None)
        try:
            token = await super().exchange_refresh_token(
                client, refresh_token, scopes
            )
        except Exception as error:
            # The email is only recovered on a *successful* upstream refresh,
            # so this line usually cannot name the user -- but a failed
            # refresh is what forces a fresh sign-in, and that sign-in logs
            # the email moments later.
            logger.warning(
                "ads_mcp auth: session refresh FAILED for %s"
                " -- client must sign in again: %s",
                _upstream_email.get() or _UNKNOWN,
                error,
            )
            raise

        logger.info(
            "ads_mcp auth: %s refreshed silently (no sign-in needed)",
            _upstream_email.get() or _UNKNOWN,
        )
        return token

    async def _try_transparent_refresh(self, upstream_token_set):
        # Where most refreshes actually happen: the proxy renews the hourly
        # Google token here on an ordinary request, without the client ever
        # calling the token endpoint.
        email = _email_from_token_set(upstream_token_set)
        try:
            refreshed = await super()._try_transparent_refresh(
                upstream_token_set
            )
        except Exception as error:
            # The proxy swallows this at debug level and falls back to
            # re-reading storage, so without this line a failing background
            # refresh is invisible -- and it is the usual reason a session
            # dies and the client has to send someone back to the consent
            # screen.
            logger.warning(
                "ads_mcp auth: background token refresh FAILED for %s"
                " -- may force a new sign-in: %s",
                email or _UNKNOWN,
                error,
            )
            raise

        logger.info(
            "ads_mcp auth: %s refreshed silently (no sign-in needed)",
            _email_from_token_set(refreshed) or email or _UNKNOWN,
        )
        return refreshed
