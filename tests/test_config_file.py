import pytest

from cf_email_unwrapper.config import load_env

PEM = '"-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"'


def write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.env").write_text(text)


def test_the_only_env_file_is_read_and_the_environment_wins(tmp_path):
    configs = tmp_path / "cf-email-unwrapper"
    write(configs, "hermes", f"AGENT_ADDRESS=hermes@example.org\nPOLL=1  # comment\nPEM={PEM}\n")
    env = load_env(None, {"XDG_CONFIG_HOME": str(tmp_path), "POLL": "2"})
    assert env["AGENT_ADDRESS"] == "hermes@example.org"
    assert env["POLL"] == "2"
    assert env["PEM"].splitlines()[1] == "AAAA"


def test_name_selects_a_file_and_several_files_need_a_name(tmp_path):
    configs = tmp_path / "cf-email-unwrapper"
    write(configs, "hermes", "AGENT_ADDRESS=hermes@example.org\n")
    write(configs, "foo", "AGENT_ADDRESS=foo@example.org\n")
    base = {"XDG_CONFIG_HOME": str(tmp_path)}
    with pytest.raises(ValueError, match="foo, hermes"):
        load_env(None, base)
    assert load_env("foo", base)["AGENT_ADDRESS"] == "foo@example.org"
    assert load_env(str(configs / "hermes.env"), base)["AGENT_ADDRESS"] == "hermes@example.org"
    with pytest.raises(ValueError, match="not found"):
        load_env("bar", base)


def test_an_exported_configuration_ignores_the_config_dir(tmp_path):
    write(tmp_path / "cf-email-unwrapper", "hermes", "ALLOWED_SENDERS=a@example.com\n")
    env = load_env(None, {"XDG_CONFIG_HOME": str(tmp_path), "AGENT_ADDRESS": "x@example.org"})
    assert "ALLOWED_SENDERS" not in env
