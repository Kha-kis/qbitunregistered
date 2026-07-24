"""Construct authenticated qBittorrent API clients."""

from typing import Any, Callable, Dict, Mapping

from qbittorrentapi import Client


def build_client_kwargs(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Build connection arguments using an API key or username/password."""
    api_key_value = config.get("api_key")
    api_key = api_key_value.strip() if isinstance(api_key_value, str) else ""

    client_kwargs: Dict[str, Any] = {"host": config["host"]}
    if api_key:
        client_kwargs["api_key"] = api_key
    else:
        client_kwargs["username"] = config["username"]
        client_kwargs["password"] = config["password"]

    return client_kwargs


def create_client(
    config: Mapping[str, Any],
    client_factory: Callable[..., Client] = Client,
) -> Client:
    """Create a qBittorrent client from validated configuration."""
    return client_factory(**build_client_kwargs(config))
