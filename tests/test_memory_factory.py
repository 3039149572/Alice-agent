from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_alice_agent.adapters.memory.factory import build_memory_port
from astrbot_plugin_alice_agent.adapters.memory.in_memory import InMemoryMemoryAdapter
from astrbot_plugin_alice_agent.adapters.memory.local_sqlite import LocalSQLiteMemoryAdapter
from astrbot_plugin_alice_agent.alice_agent.config import AgentConfig


class CapturingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def debug(self, message, *args):
        self.messages.append(str(message))

    def info(self, message, *args):
        self.messages.append(str(message))

    def warning(self, message, *args):
        self.messages.append(str(message))

    def error(self, message, *args):
        self.messages.append(str(message))

    def exception(self, message, *args):
        self.messages.append(str(message))


class MemoryFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_builds_local_database_in_plugin_data_dir(self) -> None:
        logger = CapturingLogger()
        with tempfile.TemporaryDirectory() as directory:
            port = build_memory_port(
                AgentConfig(), {}, logger, data_dir=Path(directory)
            )
            self.assertIsInstance(port, LocalSQLiteMemoryAdapter)
            self.assertTrue((Path(directory) / "alice_memory.db").exists())
            self.assertIn("network=disabled", "\n".join(logger.messages))
            await port.close()

    async def test_legacy_remote_backend_name_cannot_enable_network(self) -> None:
        logger = CapturingLogger()
        with tempfile.TemporaryDirectory() as directory:
            port = build_memory_port(
                AgentConfig(memory_backend="tencentdb"),
                {"tencent_memory_endpoint": "https://must-not-be-used.invalid"},
                logger,
                data_dir=Path(directory),
            )
            self.assertIsInstance(port, LocalSQLiteMemoryAdapter)
            self.assertTrue((Path(directory) / "alice_memory.db").exists())
            self.assertIn("using local_sqlite", "\n".join(logger.messages))
            await port.close()

    async def test_in_memory_remains_available_for_tests_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            port = build_memory_port(
                AgentConfig(memory_backend="in_memory"),
                {},
                CapturingLogger(),
                data_dir=Path(directory),
            )
            self.assertIsInstance(port, InMemoryMemoryAdapter)
            self.assertFalse((Path(directory) / "alice_memory.db").exists())

    async def test_invalid_local_config_falls_back_without_breaking_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            port = build_memory_port(
                AgentConfig(),
                {"local_memory_scope": "invalid"},
                CapturingLogger(),
                data_dir=Path(directory),
            )
            self.assertIsInstance(port, InMemoryMemoryAdapter)


if __name__ == "__main__":
    unittest.main()
