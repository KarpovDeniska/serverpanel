"""Host-key pinning parser — verifies stored-key format handling."""

import base64

import paramiko

from serverpanel.infrastructure.ssh.client import AsyncSSHClient, _host_key_line


def _fake_rsa_key() -> paramiko.RSAKey:
    return paramiko.RSAKey.generate(2048)


def test_host_key_line_roundtrips_for_rsa():
    key = _fake_rsa_key()
    line = _host_key_line(key)
    assert line.startswith("ssh-rsa ")
    # line must be two tokens separated by space
    kind, blob_b64 = line.split(None, 1)
    assert kind == "ssh-rsa"
    # base64 must decode
    raw = base64.b64decode(blob_b64)
    assert raw  # non-empty


def test_pinned_client_constructs_with_valid_line():
    """Smoke test that an AsyncSSHClient instance accepts `known_host_key=`
    without raising at construction time."""
    key = _fake_rsa_key()
    line = _host_key_line(key)
    client = AsyncSSHClient(
        host="example.invalid",
        known_host_key=line,
    )
    assert client.known_host_key == line
