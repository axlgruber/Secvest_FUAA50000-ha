"""Config Flow: Einrichtung über die HA-Oberfläche."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SecvestAuthError, SecvestClient, SecvestConnectionError
from .const import (
    CONF_DISARM_CODE,
    CONF_TRUSTED_USERS,
    CONF_PARTITIONS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): bool,
    }
)


class SecvestConfigFlow(ConfigFlow, domain=DOMAIN):
    """Einrichtungsdialog."""

    VERSION = 1

    _reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()

            client = SecvestClient(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                verify_ssl=user_input[CONF_VERIFY_SSL],
            )
            try:
                await client.async_validate()
            except SecvestAuthError:
                errors["base"] = "invalid_auth"
            except SecvestConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unerwarteter Fehler")
                errors["base"] = "unknown"
            finally:
                await client.async_close()

            if not errors:
                return self.async_create_entry(
                    title=f"Secvest ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Ausgelöst bei SecvestAuthError (z.B. nach Passwortwechsel)."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        if user_input is not None:
            client = SecvestClient(
                host=entry.data[CONF_HOST],
                port=entry.data[CONF_PORT],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            )
            try:
                await client.async_validate()
            except SecvestAuthError:
                errors["base"] = "invalid_auth"
            except SecvestConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unerwarteter Fehler")
                errors["base"] = "unknown"
            finally:
                await client.async_close()

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={
                        **entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=entry.data[CONF_USERNAME]
                ): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SecvestOptionsFlow:
        return SecvestOptionsFlow(config_entry)


class SecvestOptionsFlow(OptionsFlowWithReload):
    """Optionen: Poll-Intervall, Teilbereiche, Code, Benutzer ohne Code.

    OptionsFlowWithReload lädt die Integration nach dem Speichern neu –
    ersetzt den früheren Update-Listener (Listener + Reload-Methoden im
    Flow sind seit 2026.6 deprecated, ab 2026.12 ein Fehler).
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            code = str(user_input.get(CONF_DISARM_CODE) or "").strip()
            user_input[CONF_DISARM_CODE] = code
            return self.async_create_entry(title="", data=user_input)

        users = [
            u
            for u in await self.hass.auth.async_get_users()
            if u.is_active and not u.system_generated
        ]
        user_options = [
            SelectOptionDict(value=u.id, label=u.name or u.id) for u in users
        ]
        current_trusted = [
            uid
            for uid in self._entry.options.get(CONF_TRUSTED_USERS, [])
            if any(u.id == uid for u in users)
        ]

        coordinator = self._entry.runtime_data
        partitions = (
            coordinator.data.partitions
            if coordinator and coordinator.data
            else {}
        )
        part_options = [
            SelectOptionDict(
                value=str(pid),
                label=partitions[pid].get("name", f"Teilbereich {pid}"),
            )
            for pid in sorted(partitions)
        ]
        # Vorbelegung: gespeicherte Auswahl, sonst alle mit >=1 Zone.
        current_parts = self._entry.options.get(
            CONF_PARTITIONS,
            [str(pid) for pid, p in partitions.items() if p.get("zones")],
        )
        current_interval = self._entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=current_interval
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=5,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_PARTITIONS, default=current_parts
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=part_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Optional(
                    CONF_DISARM_CODE,
                    description={
                        "suggested_value": self._entry.options.get(
                            CONF_DISARM_CODE, ""
                        )
                    },
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Optional(
                    CONF_TRUSTED_USERS, default=current_trusted
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=user_options,
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
