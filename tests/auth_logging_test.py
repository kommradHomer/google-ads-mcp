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

"""Test cases for naming the user in sign-in and refresh logs."""

import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ads_mcp import auth_logging


def _id_token(claims) -> str:
    """Builds an unsigned stand-in for a Google OIDC id token."""

    def segment(payload):
        raw = json.dumps(payload).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature"


def _token_set(claims):
    """Builds a stand-in for a stored `UpstreamTokenSet`."""
    raw = {"access_token": "ya29.stub"}
    if claims is not None:
        raw["id_token"] = _id_token(claims)
    return SimpleNamespace(raw_token_data=raw)


class EmailFromIdTokenTest(unittest.TestCase):

    def test_reads_the_email_claim(self):
        """The address is recovered from a well-formed id token."""
        token = _id_token({"email": "roberto@example.com", "sub": "1"})
        self.assertEqual(
            auth_logging._email_from_id_token(token), "roberto@example.com"
        )

    def test_padding_is_restored(self):
        """Base64url segments arrive unpadded and must still decode."""
        # A claim set whose encoding needs padding to be a multiple of four.
        token = _id_token({"email": "a@b.co"})
        self.assertEqual(auth_logging._email_from_id_token(token), "a@b.co")

    def test_malformed_token_yields_no_email(self):
        """Garbage never raises -- logging must not break the token exchange."""
        for token in ("", "not-a-jwt", "a.b", "a.!!!.c", _id_token([1, 2])):
            with self.subTest(token=token):
                self.assertIsNone(auth_logging._email_from_id_token(token))

    def test_token_without_email_claim_yields_none(self):
        """An id token that omits `email` is not an error."""
        self.assertIsNone(auth_logging._email_from_id_token(_id_token({})))


class LoggingGoogleProviderTest(unittest.IsolatedAsyncioTestCase):
    """Exercises the overrides against a stubbed-out upstream provider."""

    def setUp(self):
        # Building a real GoogleProvider would require live OAuth config, so
        # the subclass under test is bound to a bare instance instead.
        self.provider = auth_logging.LoggingGoogleProvider.__new__(
            auth_logging.LoggingGoogleProvider
        )
        auth_logging._upstream_email.set(None)

    async def _capture_email(self, id_token=None):
        """Runs `_extract_upstream_claims` as the proxy would."""
        with patch.object(
            auth_logging.GoogleProvider,
            "_extract_upstream_claims",
            new=AsyncMock(return_value=None),
        ):
            idp_tokens = {"id_token": id_token} if id_token else {}
            await self.provider._extract_upstream_claims(idp_tokens)

    async def test_sign_in_is_logged_with_the_email(self):
        """A completed consent screen names the account that signed in."""

        async def exchange(_self, client, authorization_code):
            await self._capture_email(
                _id_token({"email": "angelo@example.com"})
            )
            return "issued-token"

        with patch.object(
            auth_logging.GoogleProvider,
            "exchange_authorization_code",
            new=exchange,
        ):
            with self.assertLogs(auth_logging.logger, "INFO") as logs:
                token = await self.provider.exchange_authorization_code(
                    None, None
                )

        self.assertEqual(token, "issued-token")
        self.assertIn("angelo@example.com", logs.output[0])
        self.assertIn("SIGNED IN", logs.output[0])

    async def test_refresh_is_logged_distinctly_from_sign_in(self):
        """A silent refresh must not read as the user typing a password."""

        async def exchange(_self, client, refresh_token, scopes):
            await self._capture_email(
                _id_token({"email": "angela@example.com"})
            )
            return "issued-token"

        with patch.object(
            auth_logging.GoogleProvider, "exchange_refresh_token", new=exchange
        ):
            with self.assertLogs(auth_logging.logger, "INFO") as logs:
                await self.provider.exchange_refresh_token(None, None, [])

        self.assertIn("angela@example.com", logs.output[0])
        self.assertIn("refreshed silently", logs.output[0])
        self.assertNotIn("SIGNED IN", logs.output[0])

    async def test_failed_refresh_is_logged_and_still_raises(self):
        """Swallowing the error would hand the client a token it never got."""

        async def exchange(_self, client, refresh_token, scopes):
            raise ValueError("invalid_grant")

        with patch.object(
            auth_logging.GoogleProvider, "exchange_refresh_token", new=exchange
        ):
            with self.assertLogs(auth_logging.logger, "WARNING") as logs:
                with self.assertRaises(ValueError):
                    await self.provider.exchange_refresh_token(None, None, [])

        self.assertIn("refresh FAILED", logs.output[0])

    async def test_background_refresh_is_logged(self):
        """Most refreshes never touch the token endpoint and must still log."""
        refreshed = _token_set({"email": "roberto@example.com"})

        async def transparent(_self, upstream_token_set):
            return refreshed

        with patch.object(
            auth_logging.GoogleProvider,
            "_try_transparent_refresh",
            new=transparent,
        ):
            with self.assertLogs(auth_logging.logger, "INFO") as logs:
                result = await self.provider._try_transparent_refresh(
                    _token_set({"email": "roberto@example.com"})
                )

        self.assertIs(result, refreshed)
        self.assertIn("roberto@example.com", logs.output[0])
        self.assertIn("refreshed silently", logs.output[0])

    async def test_failed_background_refresh_still_names_the_user(self):
        """The dead session is the complaint, so it must say whose it was."""

        async def transparent(_self, upstream_token_set):
            raise ValueError("invalid_grant")

        with patch.object(
            auth_logging.GoogleProvider,
            "_try_transparent_refresh",
            new=transparent,
        ):
            with self.assertLogs(auth_logging.logger, "WARNING") as logs:
                with self.assertRaises(ValueError):
                    await self.provider._try_transparent_refresh(
                        _token_set({"email": "angelo@example.com"})
                    )

        # The proxy only debug-logs this, so re-raising keeps its fallback.
        self.assertIn("angelo@example.com", logs.output[0])
        self.assertIn("refresh FAILED", logs.output[0])

    async def test_background_refresh_falls_back_to_the_stored_id_token(self):
        """Google may omit an id token on refresh; the old one still names them."""
        stale = _token_set({"email": "angela@example.com"})
        without_id_token = _token_set(None)

        async def transparent(_self, upstream_token_set):
            return without_id_token

        with patch.object(
            auth_logging.GoogleProvider,
            "_try_transparent_refresh",
            new=transparent,
        ):
            with self.assertLogs(auth_logging.logger, "INFO") as logs:
                await self.provider._try_transparent_refresh(stale)

        self.assertIn("angela@example.com", logs.output[0])

    async def test_unidentifiable_user_still_logs_the_event(self):
        """Losing the email must not lose the fact that a sign-in happened."""

        async def exchange(_self, client, authorization_code):
            await self._capture_email()  # no id_token in the response
            return "issued-token"

        with patch.object(
            auth_logging.GoogleProvider,
            "exchange_authorization_code",
            new=exchange,
        ):
            with self.assertLogs(auth_logging.logger, "INFO") as logs:
                await self.provider.exchange_authorization_code(None, None)

        self.assertIn(auth_logging._UNKNOWN, logs.output[0])

    async def test_email_does_not_leak_between_exchanges(self):
        """One user's address must never be attributed to the next caller."""

        async def signed_in(_self, client, authorization_code):
            await self._capture_email(
                _id_token({"email": "roberto@example.com"})
            )
            return "issued-token"

        async def no_email(_self, client, authorization_code):
            await self._capture_email()
            return "issued-token"

        with patch.object(
            auth_logging.GoogleProvider,
            "exchange_authorization_code",
            new=signed_in,
        ):
            with self.assertLogs(auth_logging.logger, "INFO"):
                await self.provider.exchange_authorization_code(None, None)

        with patch.object(
            auth_logging.GoogleProvider,
            "exchange_authorization_code",
            new=no_email,
        ):
            with self.assertLogs(auth_logging.logger, "INFO") as logs:
                await self.provider.exchange_authorization_code(None, None)

        self.assertNotIn("roberto@example.com", logs.output[0])
        self.assertIn(auth_logging._UNKNOWN, logs.output[0])


if __name__ == "__main__":
    unittest.main()
