"""Focused tests for URL parsing and the egress allow-list."""

from __future__ import annotations

import pytest

from qslcard.net import UrllibTransport
from qslcard.privacy import DEFAULT_ALLOWED_HOSTS, EgressDenied, EgressPolicy


@pytest.mark.parametrize(
    "url",
    [
        "https://xmldata.qrz.com/xml/current/",
        "https://logbook.qrz.com/api",
        "https://www.hamqth.com/xml.php",
        "https://clublog.org/realtime.php",
        "https://www.eqsl.cc/qslcard/DownloadInBox.cfm",
    ],
)
def test_official_hosts_are_allowed(url: str) -> None:
    assert EgressPolicy().check(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://qrz.com.evil.example/",
        "ftp://xmldata.qrz.com/",
        "https:///nohost",
    ],
)
def test_other_hosts_are_denied(url: str) -> None:
    with pytest.raises(EgressDenied):
        EgressPolicy().check(url)


def test_offline_mode_blocks_everything_and_allow_list_is_immutable() -> None:
    policy = EgressPolicy(offline=True)
    assert not policy.allows("https://xmldata.qrz.com/xml/current/")
    assert isinstance(DEFAULT_ALLOWED_HOSTS, frozenset)
    assert "logbook.qrz.com" in DEFAULT_ALLOWED_HOSTS


def test_transport_defaults_to_the_shared_allow_list() -> None:
    transport = UrllibTransport()
    assert transport.policy.allowed_hosts == DEFAULT_ALLOWED_HOSTS
    assert not transport.policy.offline
