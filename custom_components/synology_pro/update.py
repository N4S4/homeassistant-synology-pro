"""Update platform for Synology Pro: DSM firmware and Package Center updates."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_ALLOW_DSM_INSTALL, DOMAIN
from .services import (
    SERVICE_DOWNLOAD_DSM_UPDATE,
    SERVICE_INSTALL_DSM_UPDATE,
    SERVICE_UPDATE_PACKAGE,
    _check_dsm_update_sync,
    _list_packages_sync,
)

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(hours=6)


class SynologyUpdatesCoordinator(DataUpdateCoordinator):
    """Poll DSM firmware and Package Center updates a few times a day."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        """Initialize the update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_updates",
            update_interval=UPDATE_INTERVAL,
        )
        self.config = config

    async def _async_update_data(self) -> dict:
        """Fetch the update state in the executor."""
        return await self.hass.async_add_executor_job(self._sync_check)

    def _sync_check(self) -> dict:
        """Blocking: check DSM firmware and installed packages."""
        dsm: dict = {"available": False}
        packages: list[dict] = []
        try:
            dsm = _check_dsm_update_sync(self.config)
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("DSM update check failed: %s", exc)
        try:
            packages = _list_packages_sync(self.config).get("packages", [])
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("Package update check failed: %s", exc)
        return {"dsm": dsm, "packages": packages}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the DSM and package update entities."""
    main = hass.data[DOMAIN].get(entry.entry_id)
    if main is None:
        _LOGGER.warning("No coordinator found for update platform")
        return

    coordinator = SynologyUpdatesCoordinator(hass, main.config)
    await coordinator.async_config_entry_first_refresh()

    entities = [SynologyDSMUpdateEntity(coordinator, main, entry)]
    for package in coordinator.data.get("packages", []):
        entities.append(SynologyPackageUpdateEntity(coordinator, package))
    async_add_entities(entities)


class SynologyDSMUpdateEntity(UpdateEntity):
    """DSM firmware update entity."""

    def __init__(self, coordinator, main, entry: ConfigEntry) -> None:
        """Initialize the DSM update entity."""
        self.coordinator = coordinator
        self._main = main
        self._entry = entry
        host = main.config["host"].replace(".", "_").replace(":", "_")
        self._attr_unique_id = f"{DOMAIN}_{host}_update_dsm"
        self._attr_name = "DSM"
        self._attr_title = "DSM"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, main.config["host"])},
            "name": f"Synology NAS ({main.config['host']})",
            "manufacturer": "Synology",
        }
        self._attr_in_progress = False

    @property
    def _allow_dsm_install(self) -> bool:
        """Read the live option from the config entry manager."""
        entry = self.hass.config_entries.async_get_entry(self._entry.entry_id)
        options = entry.options if entry else self._entry.options
        return bool(options.get(CONF_ALLOW_DSM_INSTALL))

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Install is allowed only when the option is enabled."""
        if self._allow_dsm_install:
            return UpdateEntityFeature.INSTALL
        return UpdateEntityFeature(0)

    @property
    def in_progress(self) -> bool:
        """Return True while the download or install runs."""
        return self._attr_in_progress

    @property
    def installed_version(self) -> str | None:
        """Return the running DSM version."""
        sensors = self._main.data.get("sensors", {})
        for key in (
            "core_sys_info.dsm_info.version_string",
            "core_sys_info.dsm_info.version",
        ):
            value = (sensors.get(key) or {}).get("value")
            if value:
                return str(value)
        return None

    @property
    def latest_version(self) -> str | None:
        """Return the available DSM version, or the installed one."""
        dsm = self.coordinator.data.get("dsm") or {}
        if dsm.get("available"):
            return str(
                dsm.get("version") or dsm.get("title") or self.installed_version
            )
        return self.installed_version

    @property
    def release_notes(self) -> str | None:
        """Return the release notes from the DSM update server."""
        dsm = self.coordinator.data.get("dsm") or {}
        return dsm.get("release_notes")

    async def async_install(
        self, version: str | None, backup: bool, **kwargs
    ) -> None:
        """Download and install the DSM update, then the NAS reboots."""
        if not self._allow_dsm_install:
            raise HomeAssistantError(
                "DSM install from the update entity is disabled. Enable "
                "allow_dsm_install in the integration options, or use the "
                "synology_pro.install_dsm_update service with confirm: true."
            )
        dsm = self.coordinator.data.get("dsm") or {}
        if not dsm.get("available"):
            raise HomeAssistantError("No DSM update is available.")

        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            download = await self.hass.services.async_call(
                DOMAIN,
                SERVICE_DOWNLOAD_DSM_UPDATE,
                {"timeout": 1800},
                blocking=True,
                return_response=True,
            )
            if not isinstance(download, dict) or not download.get("completed"):
                error = (download or {}).get("error", "unknown error")
                raise HomeAssistantError(f"DSM download did not complete: {error}")

            install = await self.hass.services.async_call(
                DOMAIN,
                SERVICE_INSTALL_DSM_UPDATE,
                {"confirm": True},
                blocking=True,
                return_response=True,
            )
            if not isinstance(install, dict) or not install.get("success"):
                error = (install or {}).get("error", "unknown error")
                raise HomeAssistantError(f"DSM install failed: {error}")
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class SynologyPackageUpdateEntity(UpdateEntity):
    """Package Center update entity for one installed package."""

    def __init__(self, coordinator, package: dict) -> None:
        """Initialize the package update entity."""
        self.coordinator = coordinator
        self._package_id = package.get("id")
        host = coordinator.config["host"].replace(".", "_").replace(":", "_")
        safe_id = re.sub(r"[^0-9a-zA-Z._-]+", "_", str(self._package_id))
        self._attr_unique_id = f"{DOMAIN}_{host}_update_package_{safe_id}"
        self._attr_name = package.get("name") or str(self._package_id)
        self._attr_title = self._attr_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, coordinator.config["host"])},
            "name": f"Synology NAS ({coordinator.config['host']})",
            "manufacturer": "Synology",
        }
        self._attr_in_progress = False
        self._attr_supported_features = UpdateEntityFeature.INSTALL

    @property
    def _package(self) -> dict:
        """Return the current coordinator entry for this package."""
        for package in self.coordinator.data.get("packages", []):
            if package.get("id") == self._package_id:
                return package
        return {}

    @property
    def installed_version(self) -> str | None:
        """Return the installed package version."""
        return self._package.get("version")

    @property
    def in_progress(self) -> bool:
        """Return True while the package update runs."""
        return self._attr_in_progress

    @property
    def latest_version(self) -> str | None:
        """Return the available package version, or the installed one."""
        package = self._package
        return package.get("latest_version") or package.get("version")

    async def async_install(
        self, version: str | None, backup: bool, **kwargs
    ) -> None:
        """Install the available package update through the service."""
        self._attr_in_progress = True
        self.async_write_ha_state()
        try:
            result = await self.hass.services.async_call(
                DOMAIN,
                SERVICE_UPDATE_PACKAGE,
                {"package_id": self._package_id},
                blocking=True,
                return_response=True,
            )
            if not isinstance(result, dict) or not result.get("success"):
                error = (result or {}).get("error", "unknown error")
                raise HomeAssistantError(f"Package update failed: {error}")
        finally:
            self._attr_in_progress = False
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
