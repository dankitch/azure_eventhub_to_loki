import time

from logexport.azure_auth import IMDS_ENDPOINT, ManagedIdentityCredential


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))

        class R:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "access_token": "abc",
                    "expires_on": str(int(time.time()) + 3600),
                }

        return R()


def test_app_service_identity_endpoint_and_caching():
    s = FakeSession()
    env = {
        "IDENTITY_ENDPOINT": "http://localhost/msi",
        "IDENTITY_HEADER": "h",
        "AZURE_CLIENT_ID": "cid",
    }
    cred = ManagedIdentityCredential(env=env, session=s)  # type: ignore[arg-type]
    assert cred.get_token() == "abc"
    assert cred.get_token() == "abc"
    assert len(s.calls) == 1
    url, params, headers = s.calls[0]
    assert url == "http://localhost/msi"
    assert headers == {"X-IDENTITY-HEADER": "h"}
    assert params["client_id"] == "cid"
    assert params["resource"] == "https://management.azure.com/"


def test_falls_back_to_imds():
    s = FakeSession()
    ManagedIdentityCredential(env={}, session=s).get_token()  # type: ignore[arg-type]
    assert s.calls[0][0] == IMDS_ENDPOINT
    assert s.calls[0][2] == {"Metadata": "true"}
