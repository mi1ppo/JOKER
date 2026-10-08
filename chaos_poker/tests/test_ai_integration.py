import asyncio
import os

from app.config import GigaChatConfig
from app.dealer.llm import LLMUnavailable, build_llm_client


def test_build_llm_client_returns_none_without_auth_key(monkeypatch):
    monkeypatch.delenv("GIGACHAT_AUTH_KEY", raising=False)
    assert build_llm_client() is None


def test_gigachat_client_requires_a_key():
    from app.dealer.gigachat_client import GigaChatClient

    try:
        GigaChatClient(GigaChatConfig(auth_key=""))
    except LLMUnavailable:
        return
    raise AssertionError("Expected LLMUnavailable when auth key is missing")


def test_config_reads_environment_and_hides_the_key(monkeypatch):
    monkeypatch.setenv("GIGACHAT_AUTH_KEY", "c2VjcmV0OmtleQ==")
    monkeypatch.setenv("GIGACHAT_VERIFY_SSL", "false")
    monkeypatch.setenv("GIGACHAT_TOKEN_BUDGET", "1000")
    config = GigaChatConfig.from_env()
    assert config.enabled and config.verify is False and config.token_budget == 1000
    assert "c2VjcmV0" not in repr(config)


def test_load_environment_reads_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GIGACHAT_AUTH_KEY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("GIGACHAT_AUTH_KEY=hello\n", encoding="utf-8")

    import run

    run.load_environment(env_path)

    assert os.environ["GIGACHAT_AUTH_KEY"] == "hello"


def test_ensure_local_certificates_creates_missing_files(tmp_path, monkeypatch):
    cert_dir = tmp_path / "certs"
    commands = []

    def fake_run(cmd, check, **kwargs):
        commands.append(cmd)
        assert cmd[1] == "req" and check is True
        (cert_dir / "cert.pem").write_text("CERT", encoding="utf-8")
        (cert_dir / "key.pem").write_text("KEY", encoding="utf-8")

    monkeypatch.setattr("run.subprocess.run", fake_run)

    import run

    run.ensure_local_certificates(cert_dir, "192.168.1.25")

    assert (cert_dir / "cert.pem").exists()
    assert (cert_dir / "key.pem").exists()
    # The phones open https://<LAN IP>:8443, so the IP must be in subjectAltName.
    assert "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:192.168.1.25" in commands[0]

    run.ensure_local_certificates(cert_dir, "192.168.1.25")
    assert len(commands) == 1                      # same IP: reused
    run.ensure_local_certificates(cert_dir, "10.0.0.7")
    assert len(commands) == 2                      # our certificate follows the new IP


def test_user_certificate_from_mkcert_is_never_replaced(tmp_path, monkeypatch):
    cert_dir = tmp_path / "certs"
    cert_dir.mkdir()
    (cert_dir / "cert.pem").write_text("MKCERT", encoding="utf-8")
    (cert_dir / "key.pem").write_text("KEY", encoding="utf-8")

    def fail_run(cmd, check, **kwargs):
        raise AssertionError("must not regenerate a user certificate")

    monkeypatch.setattr("run.subprocess.run", fail_run)

    import run

    run.ensure_local_certificates(cert_dir, "10.0.0.7")
    assert (cert_dir / "cert.pem").read_text(encoding="utf-8") == "MKCERT"


def test_missing_openssl_gives_instructions(tmp_path, monkeypatch):
    def no_openssl(cmd, check, **kwargs):
        raise FileNotFoundError("openssl")

    monkeypatch.setattr("run.subprocess.run", no_openssl)

    import run
    import pytest

    with pytest.raises(SystemExit, match="mkcert"):
        run.ensure_local_certificates(tmp_path / "certs", "192.168.1.25")


def test_lan_ip_is_preferred_over_vpn(monkeypatch):
    import run

    monkeypatch.delenv("HOST_IP", raising=False)
    monkeypatch.setattr(run, "_route_ip", lambda: "100.66.148.223")   # VPN catches the default route
    monkeypatch.setattr(run.socket, "getaddrinfo", lambda *args: [
        (None, None, None, None, ("169.254.1.5", 0)),
        (None, None, None, None, ("192.168.137.1", 0)),
        (None, None, None, None, ("192.168.0.104", 0)),
    ])
    assert run.detect_local_ip() == "192.168.0.104"
    monkeypatch.setenv("HOST_IP", "10.0.0.5")
    assert run.detect_local_ip() == "10.0.0.5"
