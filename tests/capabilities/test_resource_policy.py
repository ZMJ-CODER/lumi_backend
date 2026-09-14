import pytest

from app.platform.security.resource_policy import ResourcePolicyError, validate_client_path, validate_command


def test_relative_workspace_path_is_allowed():
    assert validate_client_path("src/main.py") == "src/main.py"


@pytest.mark.parametrize("value", ["../secret.txt", ".env", "E:/pythonpycharm/lumi_backend/app/main.py"])
def test_sensitive_paths_are_blocked(value):
    with pytest.raises(ResourcePolicyError):
        validate_client_path(value)


def test_command_cannot_read_backend_directories():
    with pytest.raises(ResourcePolicyError):
        validate_command("Get-Content app/main.py")

