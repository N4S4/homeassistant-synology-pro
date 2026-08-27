"""Config flow for Synology Pro integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from synology_api.exceptions import LoginError

from .const import (
    CONF_DSM_VERSION,
    CONF_SCAN_INTERVAL,
    CONF_SSL,
    CONF_VERIFY_SSL,
    DEFAULT_DSM_VERSION,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class SynologyProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Synology Pro."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the initial step (user provides NAS credentials)."""
        errors = {}

        if user_input is not None:
            errors = await self._validate_credentials(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"Synology NAS ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_SSL, default=DEFAULT_SSL): bool,
                    vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
                    vol.Optional(CONF_DSM_VERSION, default=DEFAULT_DSM_VERSION): vol.In(
                        {6: "DSM 6", 7: "DSM 7"}
                    ),
                    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): int,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> FlowResult:
        """Handle reconfiguration of an existing entry.

        Pre-fills every field with the current values (except the password,
        which is never echoed back). Leaving the password blank keeps the
        existing one.
        """
        entry = self._get_reconfigure_entry()
        errors = {}

        if user_input is not None:
            # Never echo the stored password. A blank field keeps the old one.
            if not user_input.get(CONF_PASSWORD):
                user_input[CONF_PASSWORD] = entry.data.get(CONF_PASSWORD, "")
            errors = await self._validate_credentials(user_input)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST,
                        description={"suggested_value": entry.data.get(CONF_HOST)},
                    ): str,
                    vol.Required(
                        CONF_PORT,
                        description={
                            "suggested_value": entry.data.get(CONF_PORT, DEFAULT_PORT)
                        },
                    ): int,
                    vol.Required(
                        CONF_USERNAME,
                        description={"suggested_value": entry.data.get(CONF_USERNAME)},
                    ): str,
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_SSL,
                        description={
                            "suggested_value": entry.data.get(CONF_SSL, DEFAULT_SSL)
                        },
                    ): bool,
                    vol.Optional(
                        CONF_VERIFY_SSL,
                        description={
                            "suggested_value": entry.data.get(
                                CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL
                            )
                        },
                    ): bool,
                    vol.Optional(
                        CONF_DSM_VERSION,
                        description={
                            "suggested_value": entry.data.get(
                                CONF_DSM_VERSION, DEFAULT_DSM_VERSION
                            )
                        },
                    ): vol.In({6: "DSM 6", 7: "DSM 7"}),
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        description={
                            "suggested_value": entry.data.get(
                                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                            )
                        },
                    ): int,
                }
            ),
            errors=errors,
        )

    async def _validate_credentials(self, data: dict) -> dict[str, str]:
        """Test the connection and map failures to config-flow error keys."""
        errors: dict[str, str] = {}

        try:
            await self._test_connection(data)
        except ConnectionError:
            errors["base"] = "cannot_connect"
        except PermissionError:
            errors["base"] = "invalid_auth"
        except LoginError as exc:
            # 403 = 2FA code required, 406 = enforced 2FA on the account
            if exc.error_code in (403, 406):
                errors["base"] = "two_factor_required"
            else:
                _LOGGER.warning("Login failed (code %s): %s", exc.error_code, exc)
                errors["base"] = "invalid_auth"
        except Exception:
            _LOGGER.exception("Connection test failed")
            errors["base"] = "unknown"

        return errors

    async def _test_connection(self, data: dict) -> None:
        """Test the connection to the NAS (non-blocking, runs in executor)."""

        def _connect():
            from synology_api.filestation import FileStation

            fl = FileStation(
                data[CONF_HOST],
                data[CONF_PORT],
                data[CONF_USERNAME],
                data[CONF_PASSWORD],
                secure=data.get(CONF_SSL, True),
                cert_verify=data.get(CONF_VERIFY_SSL, False),
                dsm_version=data.get(CONF_DSM_VERSION, 7),
            )
            if not fl._sid:
                raise ConnectionError("Authentication failed — no session ID")
            return True

        await self.hass.async_add_executor_job(_connect)
