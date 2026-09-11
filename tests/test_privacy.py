"""Tests for credential localisation, the vault and egress control."""

from __future__ import annotations

import pytest

from qslcard.privacy import (
    CredentialVault,
    EgressDenied,
    EgressPolicy,
    audit_for_secrets,
    chacha20_block,
    chacha20_xor,
    decrypt_blob,
    encrypt_blob,
    mask_secret,
)

# RFC 8439 section 2.3.2 test vector: proves the ChaCha20 core is correct.
RFC_KEY = bytes(range(32))
RFC_NONCE = bytes.fromhex("000000090000004a00000000")
RFC_KEYSTREAM = bytes.fromhex(
    "10f1e7e4d13b5915500fdd1fa32071c4"
    "c7d1f4c733c068030422aa9ac3d46c4e"
    "d2826446079faa0914c2d705d98b02a2"
    "b5129cd1de164eb9cbd083e8a2503c4e"
)


def test_chacha20_block_matches_rfc8439() -> None:
    assert chacha20_block(RFC_KEY, 1, RFC_NONCE) == RFC_KEYSTREAM


def test_chacha20_xor_is_reversible_and_keyed() -> None:
    data = b"QSL card secrets" * 4
    once = chacha20_xor(RFC_KEY, RFC_NONCE, data)
    assert once != data
    assert chacha20_xor(RFC_KEY, RFC_NONCE, once) == data
    assert chacha20_xor(RFC_KEY, bytes(12), data) != once


def test_encrypt_decrypt_round_trip() -> None:
    blob = encrypt_blob(b"secret payload", "hunter2", iterations=1000)
    assert decrypt_blob(blob, "hunter2", iterations=1000) == b"secret payload"


def test_decrypt_rejects_wrong_password_and_tampering() -> None:
    blob = encrypt_blob(b"secret payload", "hunter2", iterations=1000)
    with pytest.raises(ValueError):
        decrypt_blob(blob, "wrong", iterations=1000)
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    with pytest.raises(ValueError):
        decrypt_blob(bytes(tampered), "hunter2", iterations=1000)


def test_egress_policy_allow_deny_offline() -> None:
    policy = EgressPolicy()
    assert policy.check("https://xmldata.qrz.com/xml/current/") == "xmldata.qrz.com"
    assert policy.allows("https://logbook.qrz.com/api")
    with pytest.raises(EgressDenied):
        policy.check("https://example.com/steal")
    with pytest.raises(EgressDenied):
        policy.check("http://xmldata.qrz.com/insecure")
    offline = EgressPolicy(offline=True)
    with pytest.raises(EgressDenied):
        offline.check("https://xmldata.qrz.com/xml/current/")
    assert not offline.allows("https://clublog.org/realtime.php")


def test_mask_secret() -> None:
    assert mask_secret("") == ""
    assert mask_secret("abcd") == "****"
    assert mask_secret("abcdefgh") == "ab****gh"


def test_vault_round_trip_with_master_password(tmp_path) -> None:
    path = tmp_path / "vault.json"
    vault = CredentialVault(str(path), master_password="master", iterations=1000)
    vault.set("qrz_password", "s3cr3t-value")
    vault.set("clublog_api", "api-key-123")
    assert vault.names() == ["clublog_api", "qrz_password"]

    reopened = CredentialVault(str(path), master_password="master", iterations=1000)
    assert reopened.get("qrz_password") == "s3cr3t-value"
    assert reopened.masked()["qrz_password"] == "s3********ue"
    assert "s3cr3t-value" not in path.read_text(encoding="utf-8")


def test_vault_rejects_wrong_master_password(tmp_path) -> None:
    path = tmp_path / "vault.json"
    CredentialVault(str(path), master_password="right", iterations=1000).set("a", "value-1")
    with pytest.raises(ValueError):
        CredentialVault(str(path), master_password="wrong", iterations=1000).load()


def test_vault_delete_clear_and_export(tmp_path) -> None:
    path = tmp_path / "vault.json"
    vault = CredentialVault(str(path), master_password="master", iterations=1000)
    vault.set("a", "value-a")
    vault.set("b", "value-b")
    assert vault.delete("a") is True
    assert vault.delete("a") is False
    bundle = tmp_path / "bundle.bin"
    vault.export_encrypted(str(bundle), "bundle-pass")
    assert b"value-b" not in bundle.read_bytes()
    target = CredentialVault(
        str(tmp_path / "other.json"), master_password="master2", iterations=1000
    )
    assert target.import_encrypted(str(bundle), "bundle-pass") == 1
    assert target.get("b") == "value-b"
    vault.clear()
    assert not path.exists()


def test_audit_detects_leaked_secret(tmp_path) -> None:
    leaked = tmp_path / "config.json"
    leaked.write_text('{"password": "s3cr3t-value"}', encoding="utf-8")
    clean = tmp_path / "log.txt"
    clean.write_text("nothing sensitive here", encoding="utf-8")
    findings = audit_for_secrets([leaked, clean], {"qrz": "s3cr3t-value"})
    assert len(findings) == 1
    assert findings[0].path.endswith("config.json")
    assert audit_for_secrets([clean], ["s3cr3t-value"]) == []
