"""日志初始化的韧性测试。"""

from pathlib import Path
from unittest.mock import patch

from app.core import logging as logging_module


def test_setup_logging_keeps_stderr_sink_when_log_directory_is_not_writable():
    """Windows bind mount 权限异常不能阻止服务启动。"""
    with (
        patch.object(Path, "mkdir", side_effect=PermissionError("read-only mount")),
        patch.object(logging_module.logger, "remove"),
        patch.object(logging_module.logger, "add") as add,
        patch.object(logging_module.logger, "warning") as warning,
    ):
        logging_module.setup_logging()

    assert add.call_count == 1
    warning.assert_called_once()
