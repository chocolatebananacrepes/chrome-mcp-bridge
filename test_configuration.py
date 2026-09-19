"""Public-package configuration checks; no browser or network is accessed."""
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Reuse the offline import shim when websocket-client is not installed.
from test_navigate_preview import bridge


class ConfigurationTests(unittest.TestCase):
    def load_configuration(self, environment):
        spec = importlib.util.spec_from_file_location("configuration_fixture", bridge.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, environment, clear=True), patch.dict(sys.modules, {"websocket": MagicMock()}):
            spec.loader.exec_module(module)
        return module

    def test_public_defaults_are_loopback(self):
        loaded = self.load_configuration({})
        self.assertEqual((loaded.BRIDGE_HOST, loaded.BRIDGE_PORT), ("127.0.0.1", 9224))
        self.assertEqual((loaded.CDP_HOST, loaded.CDP_PORT), ("127.0.0.1", 9223))

    def test_private_bind_and_port_overrides(self):
        loaded = self.load_configuration({"CHROME_MCP_HOST": "192.0.2.10", "CHROME_MCP_PORT": "18000", "CHROME_CDP_PORT": "18001"})
        self.assertEqual((loaded.BRIDGE_HOST, loaded.BRIDGE_PORT), ("192.0.2.10", 18000))
        self.assertEqual((loaded.CDP_HOST, loaded.CDP_PORT), ("127.0.0.1", 18001))

    def test_cdp_stays_local(self):
        self.assertEqual(bridge.CDP_HOST, "127.0.0.1")

    def test_default_port(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(bridge._configured_port("CHROME_MCP_PORT", 9224), 9224)

    def test_custom_port(self):
        with patch.dict(os.environ, {"CHROME_MCP_PORT": "18000"}):
            self.assertEqual(bridge._configured_port("CHROME_MCP_PORT", 9224), 18000)

    def test_invalid_port_rejected(self):
        for value in ("0", "65536", "-1", "", "invalid", "9224.5"):
            with self.subTest(value=value), patch.dict(os.environ, {"CHROME_MCP_PORT": value}):
                with self.assertRaises(ValueError):
                    bridge._configured_port("CHROME_MCP_PORT", 9224)


if __name__ == "__main__":
    unittest.main()
