"""The keys of the OAuth token response PSN issues, plus the two absolute expiry keys Curator adds to it."""

from __future__ import annotations

from typing import Final

ACCESS_TOKEN_KEY: Final = "access_token"
REFRESH_TOKEN_KEY: Final = "refresh_token"
EXPIRES_IN_KEY: Final = "expires_in"
REFRESH_TOKEN_EXPIRES_IN_KEY: Final = "refresh_token_expires_in"
SCOPE_KEY: Final = "scope"
TOKEN_TYPE_KEY: Final = "token_type"
ACCESS_TOKEN_EXPIRES_AT_KEY: Final = "access_token_expires_at"
REFRESH_TOKEN_EXPIRES_AT_KEY: Final = "refresh_token_expires_at"
