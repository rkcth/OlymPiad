"""Non-secret portable server/run settings."""

import json
from pathlib import Path
from urllib.parse import urlsplit

KEYS = {"base_url", "endpoint_model", "model_label", "concurrency", "thinking", "context_window",
        "max_tokens", "agent_timeout", "test_timeout", "engine_version", "engine_revision"}
INT_KEYS = {"concurrency", "context_window", "max_tokens", "agent_timeout", "test_timeout"}


def validate_endpoint(value):
    endpoint = urlsplit(value)
    try:
        endpoint.port
    except ValueError:
        raise ValueError("Invalid server port") from None
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError("Use an http(s) server URL without embedded credentials, query or fragment; set OLYMPIAD_API_KEY separately.")
    return value.rstrip("/")


def read_config(path):
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read benchmark config: {type(exc).__name__}") from None
    if not isinstance(data, dict) or set(data) - KEYS:
        raise ValueError("Config must contain only documented non-secret server/run settings; API keys belong in OLYMPIAD_API_KEY.")
    for key, value in data.items():
        if key in INT_KEYS:
            if type(value) is not int or value < 1:
                raise ValueError(f"Config {key} must be a positive integer")
        elif not isinstance(value, str):
            raise ValueError(f"Config {key} must be a string")
    if "base_url" in data:
        data["base_url"] = validate_endpoint(data["base_url"])
    return data
