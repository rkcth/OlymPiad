"""Bounded authenticated metadata reads; never forward keys to another origin."""

import json
import os
import urllib.request
import urllib.error
from urllib.parse import urlsplit


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        old, new = urlsplit(request.full_url), urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise ValueError("Server redirected to a different origin; configure the final server URL explicitly. No credentials were forwarded.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def read_models(base_url, timeout=15):
    key = os.environ.get("CODING_BENCHMARK_API_KEY")
    request = urllib.request.Request(base_url.rstrip("/") + "/models", headers={"Authorization": "Bearer " + key} if key else {})
    opener = urllib.request.build_opener(SameOriginRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Server model listing is too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("Server did not return an OpenAI-compatible model listing")
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in data["data"]):
        raise ValueError("Server model listing has invalid model IDs")
    return data


def check_server(base_url, model=None):
    try:
        data = read_models(base_url)
        ids = [item["id"] for item in data["data"]]
        if model:
            return {"status": "ok" if model in ids else "warning", "model": model,
                    "message": "Server metadata reachable; configured model is listed." if model in ids else "Server reachable, but configured model is not listed; gateway listings can be incomplete. Generation compatibility is not verified."}
        if len(ids) != 1:
            return {"status": "error", "model": None, "message": "Specify --endpoint-model (or --model when configuring): server does not list exactly one model."}
        return {"status": "ok", "model": ids[0], "message": "Server metadata reachable; discovered the single served model."}
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 405} and model:
            return {"status": "warning", "model": model, "message": "Gateway does not expose /models; using your explicit model ID. Generation compatibility is not verified."}
        message = "Authentication denied: set/check CODING_BENCHMARK_API_KEY and account access." if exc.code in {401, 403} else "Server metadata request failed; check the base URL (normally ending in /v1) and provider status."
        return {"status": "error", "model": None, "message": f"HTTP {exc.code}. " + message}
    except (OSError, ValueError) as exc:
        return {"status": "error", "model": None, "message": f"Server check failed ({type(exc).__name__}). Check URL, network and TLS trust; do not disable certificate verification."}
