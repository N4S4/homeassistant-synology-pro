"""Offline regression tests using the real synology-api clients.

Run: python -m unittest discover -s tests -v
Requires synology-api==0.9.5; no Home Assistant installation or NAS needed.
"""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from synology_api.base_api import BaseApi
from synology_api.core_sys_info import SysInfo


def load_coordinator():
    """Load only the coordinator, stubbing the unused HA scheduling layer."""
    root = Path(__file__).resolve().parents[1] / "custom_components/synology_pro"
    package = ModuleType("discovery_test")
    package.__path__ = [str(root)]
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    updates = ModuleType("homeassistant.helpers.update_coordinator")
    updates.DataUpdateCoordinator = object
    modules = {
        "discovery_test": package,
        "homeassistant": ModuleType("homeassistant"),
        "homeassistant.core": core,
        "homeassistant.helpers": ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.update_coordinator": updates,
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            "discovery_test.coordinator", root / "coordinator.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


coordinator = load_coordinator()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.filtered = {"SYNO.FileStation.Info": {"path": "entry.cgi", "maxVersion": 2}}
        self.full = {
            **self.filtered,
            **{name: {"path": "entry.cgi", "maxVersion": 1} for name in (
                "SYNO.Core.System", "SYNO.Core.Network",
                "SYNO.Core.Network.Router.Gateway.List",
                "SYNO.Core.Network.Proxy", "SYNO.Core.Security.Firewall.Profile",
            )},
        }
        self.request = Mock(return_value={"success": True, "data": {"status": "ok"}})
        self.session = SimpleNamespace(
            app_api_list=self.filtered, full_api_list=self.full,
            request_data=self.request, request_multi_datas=Mock(),
            sid="offline-test", base_url="https://example.invalid/webapi/",
        )
        self.shared = patch.object(BaseApi, "shared_session", self.session)
        self.shared.start()
        self.addCleanup(self.shared.stop)
        self.subject = object.__new__(coordinator.SynologyDynamicCoordinator)
        self.subject.config = {
            "host": "example.invalid", "port": 5001,
            "username": "test", "password": "unused",
        }

    def test_real_client_reproduces_file_station_catalogue_failure(self):
        api = SysInfo()
        with self.assertRaisesRegex(KeyError, "SYNO.Core.Network"):
            api.network_status()
        self.request.assert_not_called()

    def test_probe_recovers_core_apis_without_changing_shared_catalogue(self):
        methods = ["get_network_info", "network_status", "gateway_list", "proxy_info", "firewall_info"]
        sensors, _, _ = self.subject._probe_module("core_sys_info", {"class": "SysInfo", "methods": methods})
        self.assertEqual(set(sensors), {f"core_sys_info.{name}.status" for name in methods})
        self.assertEqual(self.request.call_count, len(methods))
        self.assertIs(self.session.app_api_list, self.filtered)
        self.assertEqual(list(self.filtered), ["SYNO.FileStation.Info"])
        self.assertIs(BaseApi.shared_session, self.session)
        for call in self.request.call_args_list:
            self.assertEqual(call.args[1], "entry.cgi")
            self.assertIn(call.args[2]["method"], ("get", "info", "list"))

    def test_unadvertised_api_is_not_invented_and_other_methods_continue(self):
        del self.full["SYNO.Core.Network.Router.Gateway.List"]
        sensors, _, _ = self.subject._probe_module("core_sys_info", {
            "class": "SysInfo", "methods": ["gateway_list", "network_status"],
        })
        self.assertEqual(list(sensors), ["core_sys_info.network_status.status"])
        self.assertEqual(self.request.call_count, 1)

    def test_already_complete_catalogue_still_works(self):
        self.session.app_api_list = dict(self.full)
        sensors, _, _ = self.subject._probe_module("core_sys_info", {
            "class": "SysInfo", "methods": ["network_status"],
        })
        self.assertEqual(sensors["core_sys_info.network_status.status"]["value"], "ok")


if __name__ == "__main__":
    unittest.main()
