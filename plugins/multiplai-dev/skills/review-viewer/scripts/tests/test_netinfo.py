from __future__ import annotations

import socket

from review_viewer import netinfo


def test_container_flag_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(netinfo, "DOCKERENV", tmp_path / "missing")
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    assert netinfo.detect_container()
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    (tmp_path / "dockerenv").touch()
    monkeypatch.setattr(netinfo, "DOCKERENV", tmp_path / "dockerenv")
    assert not netinfo.detect_container()
    monkeypatch.delenv("MULTIPLAI_CONTAINER")
    assert netinfo.detect_container()


def test_bind_host(monkeypatch):
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    assert netinfo.bind_host() == "127.0.0.1"
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    assert netinfo.bind_host() == "0.0.0.0"
    monkeypatch.setenv("REVIEW_VIEWER_HOST", "10.0.0.5")
    assert netinfo.bind_host() == "10.0.0.5"


def test_display_urls_plain_machine(monkeypatch):
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "0")
    assert netinfo.display_urls(8765) == [("http://127.0.0.1:8765/", "local")]


def test_display_urls_container(monkeypatch):
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    monkeypatch.setattr(netinfo, "_first_address", lambda: "192.168.1.9")
    urls = netinfo.display_urls(8766)
    assert urls[0] == (f"http://{socket.gethostname()}.orb.local:8766/", "OrbStack")
    assert urls[1][0] == "http://192.168.1.9:8766/" and "Docker Desktop" in urls[1][1]


def test_display_urls_override_first(monkeypatch):
    monkeypatch.setenv("MULTIPLAI_CONTAINER", "1")
    monkeypatch.setenv("REVIEW_VIEWER_URL_HOST", "mybox.local")
    monkeypatch.setattr(netinfo, "_first_address", lambda: None)
    urls = netinfo.display_urls(8765)
    assert urls[0][0] == "http://mybox.local:8765/"
    assert urls[1][1] == "OrbStack"
