"""Minimal managed identity token provider.

Avoids pulling in azure-identity (and its msal/cryptography dependency tree) for a single
token request. Supports the App Service / Azure Functions identity endpoint and falls back
to the Azure Instance Metadata Service (IMDS).
"""

import os
import threading
import time
from typing import Final, Mapping

import requests

ARM_RESOURCE: Final[str] = "https://management.azure.com/"
IMDS_ENDPOINT: Final[str] = "http://169.254.169.254/metadata/identity/oauth2/token"
# Refresh tokens this many seconds before they expire.
REFRESH_MARGIN_SECONDS: Final[int] = 300


class TokenError(RuntimeError):
    """Raised when a managed identity token cannot be acquired."""


class ManagedIdentityCredential:
    def __init__(
        self,
        resource: str = ARM_RESOURCE,
        env: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
    ):
        env = os.environ if env is None else env
        self.resource = resource
        self.identity_endpoint = env.get("IDENTITY_ENDPOINT")
        self.identity_header = env.get("IDENTITY_HEADER")
        # Set AZURE_CLIENT_ID to use a user-assigned identity.
        self.client_id = env.get("AZURE_CLIENT_ID")
        self.session = session or requests.Session()
        self._token: str | None = None
        self._expires_on: float = 0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_on - REFRESH_MARGIN_SECONDS:
                return self._token
            self._token, self._expires_on = self._fetch()
            return self._token

    def _fetch(self) -> tuple[str, float]:
        params = {"resource": self.resource}
        if self.client_id:
            params["client_id"] = self.client_id

        if self.identity_endpoint and self.identity_header:
            params["api-version"] = "2019-08-01"
            url = self.identity_endpoint
            headers = {"X-IDENTITY-HEADER": self.identity_header}
        else:
            params["api-version"] = "2018-02-01"
            url = IMDS_ENDPOINT
            headers = {"Metadata": "true"}

        try:
            res = self.session.get(url, params=params, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise TokenError(f"managed identity endpoint unreachable: {e}") from e
        if res.status_code != 200:
            raise TokenError(
                f"managed identity token request failed: {res.status_code} {res.text}"
            )

        body = res.json()
        try:
            expires_on = float(body["expires_on"])
        except (KeyError, ValueError):
            expires_on = time.time() + float(body.get("expires_in", 3600))
        return body["access_token"], expires_on
