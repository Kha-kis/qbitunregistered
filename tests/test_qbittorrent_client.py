"""Tests for qBittorrent client construction."""

from qbitunregistered.client import build_client_kwargs, create_client


def test_missing_api_key_uses_username_password():
    config = {
        "host": "localhost:8080",
        "username": "admin",
        "password": "password",
    }

    assert build_client_kwargs(config) == {
        "host": "localhost:8080",
        "username": "admin",
        "password": "password",
    }


def test_empty_api_key_uses_username_password():
    config = {
        "host": "localhost:8080",
        "api_key": "   ",
        "username": "admin",
        "password": "password",
    }

    assert build_client_kwargs(config) == {
        "host": "localhost:8080",
        "username": "admin",
        "password": "password",
    }


def test_api_key_takes_precedence():
    config = {
        "host": "localhost:8080",
        "api_key": "  qbt_abc123  ",
        "username": "admin",
        "password": "password",
    }

    assert build_client_kwargs(config) == {
        "host": "localhost:8080",
        "api_key": "qbt_abc123",
    }


def test_create_client_passes_resolved_arguments():
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    config = {
        "host": "localhost:8080",
        "api_key": "qbt_abc123",
    }

    client = create_client(config, client_factory=fake_client)

    assert client is not None
    assert captured == {
        "host": "localhost:8080",
        "api_key": "qbt_abc123",
    }
