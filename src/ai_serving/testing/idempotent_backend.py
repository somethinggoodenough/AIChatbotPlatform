"""Verified only against our mock server; not an assertion about any provider."""

from ai_serving.backends.providers.backend import ModelCompatBackend


class MockIdempotentBackend(ModelCompatBackend):
    supports_idempotency = True

    def _request_headers(self, req):
        headers = super()._request_headers(req)
        if req.request_id:
            headers["Idempotency-Key"] = req.request_id
        return headers
