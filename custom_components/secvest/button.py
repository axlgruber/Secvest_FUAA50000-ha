"""Button: Störungen der Secvest quittieren."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SecvestConfigEntry
from .api import SecvestError
from .const import DOMAIN
from .coordinator import SecvestCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SecvestConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([SecvestAckFaultsButton(entry.runtime_data, entry)])


class SecvestAckFaultsButton(CoordinatorEntity[SecvestCoordinator], ButtonEntity):
    """Quittiert alle aktuell gemeldeten Störungen (außer offenen Zonen)."""

    _attr_has_entity_name = True
    _attr_name = "Störungen quittieren"
    _attr_icon = "mdi:check-decagram"

    def __init__(
        self, coordinator: SecvestCoordinator, entry: SecvestConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_ack_faults"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="ABUS Secvest",
            manufacturer="ABUS",
            model="Secvest FUAA50000",
        )

    async def async_press(self) -> None:
        faults = list(self.coordinator.data.faults)
        try:
            await self.coordinator.client.async_ack_faults(faults)
        except SecvestError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_request_refresh()
