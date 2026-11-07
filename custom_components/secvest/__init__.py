"""ABUS Secvest Integration für Home Assistant."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    Platform,
)
from homeassistant.core import HomeAssistant

from .api import SecvestClient
from .const import CONF_PARTITIONS, CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL, DOMAIN
from .coordinator import SecvestCoordinator

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

type SecvestConfigEntry = ConfigEntry[SecvestCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: SecvestConfigEntry) -> bool:
    """Config Entry laden."""
    client = SecvestClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
    )
    coordinator = SecvestCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: SecvestConfigEntry
) -> None:
    """Bei Optionsänderung (Intervall/Teilbereiche) neu laden."""
    await hass.config_entries.async_reload(entry.entry_id)


def enabled_partitions(
    entry: SecvestConfigEntry, coordinator: SecvestCoordinator
) -> set[int]:
    """Aktive Teilbereich-IDs laut Optionen; Default: alle mit >=1 Zone."""
    selected = entry.options.get(CONF_PARTITIONS)
    if selected is None:
        return {
            pid
            for pid, part in coordinator.data.partitions.items()
            if part.get("zones")
        }
    return {int(pid) for pid in selected}


async def async_unload_entry(hass: HomeAssistant, entry: SecvestConfigEntry) -> bool:
    """Config Entry entladen."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.client.async_close()
    return unload_ok
