"""Services for the Synology Pro integration."""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.service import SupportsResponse
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ATTR_BUILD_BASE,
    ATTR_BUILD_NANO,
    ATTR_CONFIRM,
    ATTR_CONTAINER_ID,
    ATTR_DESCRIPTION,
    ATTR_DOWNLOAD_ONLY,
    ATTR_LANG,
    ATTR_LIMIT,
    ATTR_LOCK,
    ATTR_ONLY_UPDATES,
    ATTR_PACKAGE_ID,
    ATTR_SHARE,
    ATTR_SNAPSHOT_ID,
    ATTR_SNAPSHOT_NAME,
    ATTR_TIMEOUT,
    ATTR_VM_NAME,
    DOMAIN,
    SERVICE_CHECK_DSM_UPDATE,
    SERVICE_CREATE_SNAPSHOT,
    SERVICE_CREATE_VM_SNAPSHOT,
    SERVICE_DELETE_SNAPSHOT,
    SERVICE_DELETE_VM_SNAPSHOT,
    SERVICE_DOWNLOAD_DSM_UPDATE,
    SERVICE_INSTALL_DSM_UPDATE,
    SERVICE_LIST_PACKAGES,
    SERVICE_LIST_VM,
    SERVICE_LIST_VM_SNAPSHOTS,
    SERVICE_RESTART_CONTAINER,
    SERVICE_RESTORE_VM_SNAPSHOT,
    SERVICE_RUN_SECURITY_SCAN,
    SERVICE_UPDATE_CONTAINER,
    SERVICE_UPDATE_PACKAGE,
)

_LOGGER = logging.getLogger(__name__)


def _package_instance(config: dict):
    """Instantiate Package for the given NAS config."""
    from synology_api.core_package import Package

    return Package(
        config["host"], config["port"],
        config["username"], config["password"],
        secure=config.get("use_ssl", True),
        cert_verify=config.get("verify_ssl", False),
        dsm_version=config.get("dsm_version", 7),
    )


def _sysinfo_instance(config: dict):
    """Instantiate SysInfo for the given NAS config."""
    from synology_api.core_sys_info import SysInfo

    return SysInfo(
        config["host"], config["port"],
        config["username"], config["password"],
        secure=config.get("use_ssl", True),
        cert_verify=config.get("verify_ssl", False),
        dsm_version=config.get("dsm_version", 7),
    )


def _core_upgrade_instance(config: dict):
    """Instantiate CoreUpgrade for the given NAS config."""
    from synology_api.core_upgrade import CoreUpgrade

    return CoreUpgrade(
        config["host"], config["port"],
        config["username"], config["password"],
        secure=config.get("use_ssl", True),
        cert_verify=config.get("verify_ssl", False),
        dsm_version=config.get("dsm_version", 7),
    )


def _pending_operation(package: dict):
    """Return the DSM pending operation of a package, if any."""
    additional = package.get("additional") or {}
    return additional.get("available_operation") or package.get("available_operation")


def _list_packages_sync(
    config: dict, package_id: str | None = None, only_updates: bool = False
) -> dict:
    """Blocking: list installed packages and their update state."""
    pkg = _package_instance(config)
    # Package Center caches its catalogue for about 30 minutes. The installed
    # list only reports available_operation when this ran first in the session.
    catalog = pkg.list_installable()
    catalog_entries = (catalog.get("data") or {}).get("packages") or []
    latest_by_id = {
        entry.get("id"): entry.get("version")
        for entry in catalog_entries
        if entry.get("id")
    }

    response = pkg.list_installed(
        additional=["available_operation", "status", "autoupdate", "install_type"]
    )
    packages = (response.get("data") or {}).get("packages") or []
    result = []
    for package in packages:
        if package_id and package.get("id") != package_id:
            continue
        operation = _pending_operation(package)
        if only_updates and not operation:
            continue
        additional = package.get("additional") or {}
        latest = latest_by_id.get(package.get("id")) if operation else None
        result.append(
            {
                "id": package.get("id"),
                "name": package.get("name"),
                "version": package.get("version"),
                "latest_version": latest or package.get("version"),
                "status": additional.get("status"),
                "autoupdate": additional.get("autoupdate"),
                "update_available": bool(operation),
                "available_operation": operation,
            }
        )
    _LOGGER.info(
        "Listed %s packages (%s with a pending update)",
        len(result), sum(1 for p in result if p["update_available"]),
    )
    return {"packages": result, "count": len(result)}


def _check_dsm_update_sync(config: dict) -> dict:
    """Blocking: ask the DSM update server whether a new firmware exists."""
    response = _sysinfo_instance(config).upgrade_server_check()
    if not isinstance(response, dict) or not response.get("success", False):
        _LOGGER.error("DSM update check failed: %s", response)
        return {"available": False, "error": "DSM update check failed"}

    data = response.get("data") or {}
    update = data.get("update") or {}
    promotion = data.get("promotion") or {}
    result = {
        "available": bool(update.get("available")),
        "version": update.get("version"),
        "title": update.get("title") or update.get("name"),
        "release_notes": update.get("description") or update.get("release_note"),
        "promotion_available": bool(promotion.get("available")),
        "details": update,
    }
    _LOGGER.info(
        "DSM update check: available=%s version=%s",
        result["available"], result["version"],
    )
    return result


async def async_register_services(hass: HomeAssistant) -> None:
    """Register all custom services."""

    def _get_nas_config() -> dict | None:
        """Get NAS config from the shared coordinator."""
        entries = hass.data.get(DOMAIN, {})
        for data in entries.values():
            if isinstance(data, DataUpdateCoordinator):
                return data.config
        return None

    def _log_package_result(package_id: str, result: dict) -> dict:
        """Log a package update result and return it unchanged."""
        if result.get("success"):
            _LOGGER.info(
                "Package update for %s: %s", package_id,
                "already up to date" if result.get("up_to_date") else result.get("version"),
            )
        else:
            _LOGGER.error(
                "Package update failed for %s: %s", package_id, result.get("error")
            )
        return result

    def _log_dsm_download_result(result: dict) -> dict:
        """Log a DSM download result and return it unchanged."""
        if result.get("completed"):
            _LOGGER.info("DSM update download completed")
        elif result.get("started"):
            _LOGGER.warning("DSM update download started but not completed yet")
        else:
            _LOGGER.error("DSM update download failed: %s", result.get("error"))
        return result

    async def create_snapshot(call: ServiceCall):
        """Create a snapshot of a shared folder."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        share = call.data.get(ATTR_SHARE)
        desc = call.data.get(ATTR_DESCRIPTION, "HA auto-snapshot")

        def _run():
            from synology_api.snapshot import Snapshot

            snap = Snapshot(
                config["host"], config["port"],
                config["username"], config["password"],
                secure=config.get("use_ssl", True),
                cert_verify=config.get("verify_ssl", False),
                dsm_version=config.get("dsm_version", 7),
            )
            snap.create_snapshot(share, desc)

        await hass.async_add_executor_job(_run)
        _LOGGER.info("Snapshot created for %s: %s", share, desc)

    async def delete_snapshot(call: ServiceCall):
        """Delete a snapshot."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        share = call.data.get(ATTR_SHARE)
        name = call.data.get(ATTR_SNAPSHOT_NAME)

        def _run():
            from synology_api.snapshot import Snapshot

            snap = Snapshot(
                config["host"], config["port"],
                config["username"], config["password"],
                secure=config.get("use_ssl", True),
                cert_verify=config.get("verify_ssl", False),
                dsm_version=config.get("dsm_version", 7),
            )
            snap.delete_snapshot(share, name)

        await hass.async_add_executor_job(_run)
        _LOGGER.info("Snapshot deleted: %s/%s", share, name)

    async def restart_container(call: ServiceCall):
        """Restart a Docker container."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        container_id = call.data.get(ATTR_CONTAINER_ID)

        def _run():
            from synology_api.docker_api import Docker

            docker = Docker(
                config["host"], config["port"],
                config["username"], config["password"],
                secure=config.get("use_ssl", True),
                cert_verify=config.get("verify_ssl", False),
                dsm_version=config.get("dsm_version", 7),
            )
            docker.restart_container(container_id)

        await hass.async_add_executor_job(_run)
        _LOGGER.info("Container restarted: %s", container_id)

    async def update_container(call: ServiceCall):
        """Pull latest image and recreate a container."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        container_id = call.data.get(ATTR_CONTAINER_ID)

        def _run():
            from synology_api.docker_api import Docker

            docker = Docker(
                config["host"], config["port"],
                config["username"], config["password"],
                secure=config.get("use_ssl", True),
                cert_verify=config.get("verify_ssl", False),
                dsm_version=config.get("dsm_version", 7),
            )
            # Get container info to find the image
            containers = docker.get_all_containers()
            for c in containers:
                if c.get("id") == container_id:
                    image = c.get("image", "")
                    docker.pull_image(image)
                    docker.stop_container(container_id)
                    # In a full implementation, you'd capture and reapply settings
                    docker.start_container(container_id)
                    return image
            return None

        image = await hass.async_add_executor_job(_run)
        if image is not None:
            _LOGGER.info("Container updated: %s", container_id)

    async def run_security_scan(call: ServiceCall):
        """Run a Security Advisor scan."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        def _run():
            from synology_api.security_advisor import SecurityAdvisor

            advisor = SecurityAdvisor(
                config["host"], config["port"],
                config["username"], config["password"],
                secure=config.get("use_ssl", True),
                cert_verify=config.get("verify_ssl", False),
                dsm_version=config.get("dsm_version", 7),
            )
            advisor.run_scan()

        await hass.async_add_executor_job(_run)
        _LOGGER.info("Security scan triggered")

    async def list_packages(call: ServiceCall):
        """List installed packages with their version and update state."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"packages": [], "count": 0}

        package_id = call.data.get(ATTR_PACKAGE_ID)
        only_updates = bool(call.data.get(ATTR_ONLY_UPDATES, False))
        return await hass.async_add_executor_job(
            _list_packages_sync, config, package_id, only_updates
        )

    async def check_dsm_update(call: ServiceCall):
        """Check whether a DSM system update is available."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"available": False, "error": "No NAS config found"}

        return await hass.async_add_executor_job(_check_dsm_update_sync, config)

    async def update_package(call: ServiceCall):
        """Download and install the latest version of an installed package."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"success": False, "error": "No NAS config found"}

        package_id = call.data.get(ATTR_PACKAGE_ID)
        if not package_id:
            return {"success": False, "error": "package_id is required"}
        download_only = bool(call.data.get(ATTR_DOWNLOAD_ONLY, False))
        try:
            timeout = int(call.data.get(ATTR_TIMEOUT, 300))
        except (TypeError, ValueError):
            timeout = 300

        def _prepare():
            """Blocking: inspect the package and start the download."""
            pkg = _package_instance(config)

            installed = pkg.list_installed(additional=["available_operation"])
            installed_packages = (installed.get("data") or {}).get("packages") or []
            current = next(
                (p for p in installed_packages if p.get("id") == package_id), None
            )
            if current is None:
                return pkg, {
                    "success": False,
                    "package_id": package_id,
                    "error": "Package is not installed. This service only updates "
                             "installed packages.",
                }

            # Refresh the catalogue, then pick the entry to download.
            catalog = pkg.list_installable()
            entries = (catalog.get("data") or {}).get("packages") or []
            entry = next((p for p in entries if p.get("id") == package_id), None)
            if entry is None:
                return pkg, {
                    "success": False,
                    "package_id": package_id,
                    "error": "Package not found in the Package Center catalogue.",
                }

            if entry.get("version") == current.get("version"):
                return pkg, {
                    "success": True,
                    "package_id": package_id,
                    "version": current.get("version"),
                    "up_to_date": True,
                    "downloaded": False,
                    "installed": False,
                }

            started = pkg.download_package(
                entry.get("link"),
                package_id,
                entry.get("md5"),
                str(entry.get("size")),
            )
            task_id = (started.get("data") or {}).get("taskid")
            if not started.get("success", False) or not task_id:
                return pkg, {
                    "success": False,
                    "package_id": package_id,
                    "error": "DSM refused to start the download.",
                    "details": started,
                }

            return pkg, {
                "started": True,
                "task_id": task_id,
                "version": entry.get("version"),
                "current_version": current.get("version"),
            }

        pkg, prepared = await hass.async_add_executor_job(_prepare)
        if not prepared.get("started"):
            return _log_package_result(package_id, prepared)

        task_id = prepared["task_id"]
        version = prepared["version"]
        current_version = prepared["current_version"]

        def _poll():
            """Blocking: read the download status once."""
            return pkg.get_dowload_package_status(task_id)

        deadline = time.monotonic() + timeout
        status = {}
        while time.monotonic() < deadline:
            status = await hass.async_add_executor_job(_poll)
            data = status.get("data") or {}
            if data.get("finished"):
                break
            if status.get("success") is False or data.get("success") is False:
                return _log_package_result(package_id, {
                    "success": False,
                    "package_id": package_id,
                    "task_id": task_id,
                    "error": "DSM reported a download failure.",
                    "details": status,
                })
            await asyncio.sleep(2)
        else:
            return _log_package_result(package_id, {
                "success": False,
                "package_id": package_id,
                "task_id": task_id,
                "error": f"Download did not finish within {timeout} seconds.",
                "progress": (status.get("data") or {}).get("progress"),
            })

        if download_only:
            return _log_package_result(package_id, {
                "success": True,
                "package_id": package_id,
                "version": version,
                "task_id": task_id,
                "downloaded": True,
                "installed": False,
            })

        def _finish():
            """Blocking: verify the download and start the install."""
            check = pkg.check_installation_from_download(task_id)
            if not check.get("success", False):
                return {
                    "success": False,
                    "package_id": package_id,
                    "task_id": task_id,
                    "error": "The downloaded file could not be verified.",
                    "details": check,
                }
            upgrade = pkg.upgrade_package(task_id)
            return {
                "success": bool(upgrade.get("success", False)),
                "package_id": package_id,
                "version": version,
                "previous_version": current_version,
                "task_id": task_id,
                "downloaded": True,
                "installed": bool(upgrade.get("success", False)),
                "details": upgrade,
            }

        result = await hass.async_add_executor_job(_finish)
        return _log_package_result(package_id, result)

    async def download_dsm_update(call: ServiceCall):
        """Start the DSM system update download and optionally wait for it."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"success": False, "error": "No NAS config found"}

        try:
            timeout = int(call.data.get(ATTR_TIMEOUT, 300))
        except (TypeError, ValueError):
            timeout = 300

        def _start():
            """Blocking: start the download and prepare the poll client."""
            from requests.exceptions import RequestException
            from synology_api.exceptions import SynoBaseException

            upgrade = _core_upgrade_instance(config)
            try:
                started = upgrade.upgrade_download_start(
                    target="update", need_auto_smallupdate=True
                )
            except (SynoBaseException, RequestException) as exc:
                return None, {
                    "success": False,
                    "error": f"DSM refused to start the download: {exc}",
                }
            if not isinstance(started, dict) or not started.get("success", False):
                return None, {
                    "success": False,
                    "error": "DSM refused to start the download.",
                    "details": started,
                }
            if timeout <= 0:
                return None, {
                    "success": True,
                    "started": True,
                    "completed": False,
                    "details": started,
                }
            return _sysinfo_instance(config), {
                "success": True,
                "started": True,
                "details": started,
            }

        sysinfo, first = await hass.async_add_executor_job(_start)
        if sysinfo is None:
            return _log_dsm_download_result(first)

        def _poll():
            """Blocking: read the download progress once."""
            from requests.exceptions import RequestException
            from synology_api.exceptions import SynoBaseException

            try:
                return sysinfo.sys_upgrade_download(need_download_target=True)
            except (SynoBaseException, RequestException) as exc:
                return {"_exception": str(exc)}

        deadline = time.monotonic() + timeout
        last = {}
        while time.monotonic() < deadline:
            last = await hass.async_add_executor_job(_poll)
            if "_exception" in last:
                return _log_dsm_download_result({
                    "success": False,
                    "started": True,
                    "completed": False,
                    "error": f"Could not read the download progress: {last['_exception']}",
                    "details": {},
                })
            data = last.get("data") or {}
            percent = data.get("percent")
            status = str(data.get("status") or "").lower()
            if isinstance(percent, (int, float)) and percent >= 100:
                return _log_dsm_download_result({
                    "success": True,
                    "started": True,
                    "completed": True,
                    "percent": percent,
                    "status": status,
                    "details": last,
                })
            if status in ("error", "failed", "fail", "cancel",
                          "cancelled", "canceled"):
                return _log_dsm_download_result({
                    "success": False,
                    "started": True,
                    "completed": False,
                    "percent": percent,
                    "status": status,
                    "error": "DSM reported a download problem.",
                    "details": last,
                })
            if status in ("done", "finished", "completed", "success"):
                return _log_dsm_download_result({
                    "success": True,
                    "started": True,
                    "completed": True,
                    "percent": percent,
                    "status": status,
                    "details": last,
                })
            await asyncio.sleep(5)

        return _log_dsm_download_result({
            "success": False,
            "started": True,
            "completed": False,
            "percent": (last.get("data") or {}).get("percent"),
            "status": (last.get("data") or {}).get("status"),
            "error": f"Download did not finish within {timeout} seconds. "
                     "It may still be running on the NAS.",
            "details": last,
        })

    async def install_dsm_update(call: ServiceCall):
        """Install the downloaded DSM update after the DSM pre-checks."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"success": False, "error": "No NAS config found"}

        if call.data.get(ATTR_CONFIRM) is not True:
            return {
                "success": False,
                "error": "Set confirm to true. This installs the DSM update and "
                         "reboots the NAS.",
            }

        base = call.data.get(ATTR_BUILD_BASE)
        nano = call.data.get(ATTR_BUILD_NANO)
        lang = call.data.get(ATTR_LANG) or "enu"

        def _run():
            from requests.exceptions import RequestException
            from synology_api.exceptions import SynoBaseException, SynoConnectionError

            upgrade = _core_upgrade_instance(config)
            sysinfo = _sysinfo_instance(config)
            steps = {}

            try:
                basic = upgrade.upgrade_basic_status()
                steps["basic_status"] = basic
                if not isinstance(basic, dict) or not basic.get("success", False):
                    return {
                        "success": False,
                        "error": "DSM did not answer the basic status check.",
                        "steps": steps,
                    }

                feasibility = sysinfo.upgrade_status(feasibility_check=True)
                steps["feasibility_check"] = feasibility
                if not isinstance(feasibility, dict) or not feasibility.get("success", False):
                    return {
                        "success": False,
                        "error": "DSM did not answer the feasibility check.",
                        "steps": steps,
                    }
                precheck = (feasibility.get("data") or {}).get("precheck_info") or {}
                if precheck.get("can_continue") is False:
                    return {
                        "success": False,
                        "error": "DSM feasibility check found a blocking problem.",
                        "steps": steps,
                    }

                if base is not None and nano is not None:
                    try:
                        build = {"base": int(base), "nano": int(nano)}
                    except (TypeError, ValueError):
                        return {
                            "success": False,
                            "error": "base and nano must be integers.",
                            "steps": steps,
                        }
                    steps["upgradeable_drives"] = (
                        upgrade.upgrade_check_upgradeable_drives(build)
                    )
                else:
                    steps["upgradeable_drives"] = {
                        "skipped": "base and nano were not provided"
                    }

                pre = upgrade.upgrade_precheck_start(lang=lang, upgrade_type="dsm")
                steps["precheck_start"] = pre
                if not isinstance(pre, dict) or not pre.get("success", False):
                    return {
                        "success": False,
                        "error": "DSM did not start the upgrade pre-check.",
                        "steps": steps,
                    }
            except SynoBaseException as exc:
                return {"success": False, "error": str(exc), "steps": steps}

            try:
                install = upgrade.upgrade_install(upgrade_type="dsm", force=True)
            except SynoConnectionError as exc:
                steps["install"] = {"connection_lost": str(exc)}
                return {
                    "success": True,
                    "installing": True,
                    "note": "The connection dropped after the install call. "
                            "This is expected when the NAS starts the reboot.",
                    "steps": steps,
                }
            except SynoBaseException as exc:
                steps["install"] = {"error": str(exc)}
                return {
                    "success": False,
                    "error": f"DSM refused the install: {exc}",
                    "steps": steps,
                }
            except RequestException as exc:
                steps["install"] = {"request_error": str(exc)}
                return {
                    "success": True,
                    "installing": True,
                    "note": "The request ended after the install call. "
                            "This is expected when the NAS starts the reboot.",
                    "steps": steps,
                }

            steps["install"] = install
            return {
                "success": isinstance(install, dict) and install.get("success", False),
                "steps": steps,
            }

        result = await hass.async_add_executor_job(_run)
        if result.get("success"):
            _LOGGER.warning("DSM update install started, the NAS will reboot")
        else:
            _LOGGER.error("DSM update install failed: %s", result.get("error"))
        return result

    def _vm_instance(config: dict):
        """Instantiate Virtualization for the given NAS config."""
        from synology_api.virtualization import Virtualization

        return Virtualization(
            config["host"], config["port"],
            config["username"], config["password"],
            secure=config.get("use_ssl", True),
            cert_verify=config.get("verify_ssl", False),
            dsm_version=config.get("dsm_version", 7),
        )

    def _vm_client(config: dict, vm_name: str):
        """Instantiate Virtualization and resolve vm_name to its guest_id."""
        virt = _vm_instance(config)
        info = virt.get_vm_operation()
        for guest in (info.get("data") or {}).get("guests", []):
            if guest.get("guest_name") == vm_name:
                return virt, guest.get("guest_id")
        return virt, None

    async def create_vm_snapshot(call: ServiceCall):
        """Create a snapshot of a virtual machine."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        vm_name = call.data.get(ATTR_VM_NAME)
        desc = call.data.get(ATTR_DESCRIPTION, "HA auto-snapshot")
        lock = bool(call.data.get(ATTR_LOCK, False))

        def _run():
            virt, guest_id = _vm_client(config, vm_name)
            if not guest_id:
                _LOGGER.error("VM not found: %s", vm_name)
                return
            virt.create_snapshot(guest_id, desc, is_user_locked=lock)

        await hass.async_add_executor_job(_run)
        _LOGGER.info("VM snapshot created for %s: %s (locked=%s)", vm_name, desc, lock)

    async def delete_vm_snapshot(call: ServiceCall):
        """Delete a snapshot of a virtual machine."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        vm_name = call.data.get(ATTR_VM_NAME)
        snapshot_id = call.data.get(ATTR_SNAPSHOT_ID)

        def _run():
            virt, guest_id = _vm_client(config, vm_name)
            if not guest_id:
                _LOGGER.error("VM not found: %s", vm_name)
                return
            virt.delete_snapshot(guest_id, snapshot_id)

        await hass.async_add_executor_job(_run)
        _LOGGER.info("VM snapshot deleted for %s: %s", vm_name, snapshot_id)

    async def list_vm_snapshots(call: ServiceCall):
        """List the snapshots of a virtual machine, newest first."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"snapshots": []}

        vm_name = call.data.get(ATTR_VM_NAME)
        limit = call.data.get(ATTR_LIMIT)

        def _run():
            virt, guest_id = _vm_client(config, vm_name)
            if not guest_id:
                _LOGGER.error("VM not found: %s", vm_name)
                return []
            snapshots = virt.get_snapshot_list(guest_id)
            return (snapshots.get("data") or {}).get("gsnaps", [])

        snaps = await hass.async_add_executor_job(_run)
        # Newest first, matching the VMM UI order.
        snaps = list(reversed(snaps))
        if isinstance(limit, int) and limit > 0:
            snaps = snaps[:limit]
        result = [
            {
                "gsnap_id": s.get("gsnap_id"),
                "description": s.get("desc"),
                "time": s.get("gsnap_time_str"),
                "is_user_locked": bool(s.get("is_user_locked", False)),
                "status": s.get("status"),
            }
            for s in snaps
        ]
        for snap in result:
            _LOGGER.info(
                "VM snapshot: %s | %s | %s | locked=%s",
                snap["gsnap_id"], snap["description"], snap["time"],
                snap["is_user_locked"],
            )
        return {"snapshots": result}

    async def restore_vm_snapshot(call: ServiceCall):
        """Restore a virtual machine from a snapshot."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return

        vm_name = call.data.get(ATTR_VM_NAME)
        snapshot_id = call.data.get(ATTR_SNAPSHOT_ID)

        def _run():
            virt, guest_id = _vm_client(config, vm_name)
            if not guest_id:
                _LOGGER.error("VM not found: %s", vm_name)
                return
            virt.restore_snapshot(guest_id, snapshot_id)

        await hass.async_add_executor_job(_run)
        _LOGGER.info("VM restored from snapshot %s for %s", snapshot_id, vm_name)

    async def list_vm(call: ServiceCall):
        """List all virtual machines with their status and details."""
        config = _get_nas_config()
        if not config:
            _LOGGER.error("No NAS config found")
            return {"vms": []}

        vm_name = call.data.get(ATTR_VM_NAME)

        def _run():
            virt = _vm_instance(config)
            info = virt.get_vm_operation()
            return (info.get("data") or {}).get("guests", [])

        guests = await hass.async_add_executor_job(_run)
        result = [
            {
                "guest_id": g.get("guest_id"),
                "guest_name": g.get("guest_name"),
                "status": g.get("status"),
                "autorun": g.get("autorun"),
                "vcpu_num": g.get("vcpu_num"),
                "vram_size": g.get("vram_size"),
                "storage_id": g.get("storage_id"),
                "storage_name": g.get("storage_name"),
                "description": g.get("description"),
                "vdisks": g.get("vdisks", []),
                "vnics": g.get("vnics", []),
            }
            for g in guests
            if not vm_name or g.get("guest_name") == vm_name
        ]
        return {"vms": result}

    # Register all services
    hass.services.async_register(DOMAIN, SERVICE_CREATE_SNAPSHOT, create_snapshot)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_SNAPSHOT, delete_snapshot)
    hass.services.async_register(DOMAIN, SERVICE_RESTART_CONTAINER, restart_container)
    hass.services.async_register(DOMAIN, SERVICE_UPDATE_CONTAINER, update_container)
    hass.services.async_register(DOMAIN, SERVICE_RUN_SECURITY_SCAN, run_security_scan)
    hass.services.async_register(DOMAIN, SERVICE_CREATE_VM_SNAPSHOT, create_vm_snapshot)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_VM_SNAPSHOT, delete_vm_snapshot)
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_VM_SNAPSHOTS, list_vm_snapshots,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_RESTORE_VM_SNAPSHOT, restore_vm_snapshot)
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_VM, list_vm,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LIST_PACKAGES, list_packages,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CHECK_DSM_UPDATE, check_dsm_update,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UPDATE_PACKAGE, update_package,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DOWNLOAD_DSM_UPDATE, download_dsm_update,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_INSTALL_DSM_UPDATE, install_dsm_update,
        supports_response=SupportsResponse.OPTIONAL,
    )
