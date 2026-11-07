"""Binary Sensors: Störungsstatus pro Teilbereich.

"Ein" bedeutet: mindestens eine Störung betrifft diesen Teilbereich
(z.B. offene Draht-Zone), die das Scharfschalten verhindern kann.
Details stehen in den Attributen.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SecvestConfigEntry, enabled_partitions
from .const import DOMAIN, FAULT_TYPE_OPEN_ZONE, FAULT_TYPES
from .coordinator import SecvestCoordinator


def _maintenance_faults(
    faults: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Störungen, die Wartung bedeuten: Funk-/Batteriewarnung oder jede
    Nicht-offene-Zone-Störung (unbekannte Codes generisch einbeziehen)."""
    return [
        f
        for f in faults
        if f.get("is-rf-warning")
        or str(f.get("type")) != FAULT_TYPE_OPEN_ZONE
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SecvestConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    enabled = enabled_partitions(entry, coordinator)

    def _zone_enabled(zid: str) -> bool:
        part = coordinator.data.zones.get(zid, {}).get("partition")
        try:
            return int(part) in enabled
        except (TypeError, ValueError):
            return True  # ohne bekannten Teilbereich lieber anzeigen

    entities: list[BinarySensorEntity] = [
        SecvestFaultSensor(coordinator, entry, pid)
        for pid in sorted(coordinator.data.partitions)
        if pid in enabled
    ]
    entities += [
        SecvestZoneSensor(coordinator, entry, zid)
        for zid in sorted(coordinator.data.zones)
        if _zone_enabled(zid)
    ]
    entities.append(SecvestMaintenanceSensor(coordinator, entry))
    async_add_entities(entities)


class SecvestFaultSensor(CoordinatorEntity[SecvestCoordinator], BinarySensorEntity):
    """Störungssensor eines Teilbereichs."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: SecvestCoordinator,
        entry: SecvestConfigEntry,
        partition: int,
    ) -> None:
        super().__init__(coordinator)
        self._partition = partition
        self._attr_unique_id = f"{entry.entry_id}_fault_{partition}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    @property
    def name(self) -> str:
        part = self.coordinator.data.partitions.get(self._partition)
        base = part["name"] if part else f"Teilbereich {self._partition}"
        return f"Störung {base}"

    def _faults(self) -> list[dict[str, Any]]:
        pid = str(self._partition)
        return [
            f
            for f in self.coordinator.data.faults
            if pid in f.get("affects-partition", [])
        ]

    @property
    def is_on(self) -> bool:
        return len(self._faults()) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        faults = self._faults()
        return {
            "meldungen": [f.get("ui-string", "?") for f in faults],
            "verhindert_scharfschalten": any(
                f.get("prevents-set") for f in faults
            ),
        }


class SecvestZoneSensor(CoordinatorEntity[SecvestCoordinator], BinarySensorEntity):
    """Einzelner Melder/eine Zone (offen/geschlossen).

    Offen = Endpoint /system/partitions-{n}/zones/ meldet "open" ODER die
    Zone erscheint als offene-Zone-Störung (type 5000).
    Name wird beim ersten Offen-Zustand aus der Störung übernommen und
    im Client gecacht; bis dahin "Zone <id>". unique_id über die
    stabile Zonen-ID, nicht den Namen.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.OPENING

    def __init__(
        self,
        coordinator: SecvestCoordinator,
        entry: SecvestConfigEntry,
        zone_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._zone = str(zone_id)
        self._attr_unique_id = f"{entry.entry_id}_zone_{self._zone}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    def _zone_data(self) -> dict[str, Any]:
        return self.coordinator.data.zones.get(self._zone, {})

    @property
    def name(self) -> str:
        return self._zone_data().get("name") or f"Zone {self._zone}"

    @property
    def is_on(self) -> bool:
        return bool(self._zone_data().get("open"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zone = self._zone_data()
        return {
            "zone_id": self._zone,
            "teilbereich": zone.get("partition"),
        }


class SecvestMaintenanceSensor(
    CoordinatorEntity[SecvestCoordinator], BinarySensorEntity
):
    """Anlagenweiter Wartungssensor (Funk-/Batterie-/sonstige Störungen).

    "Ein" bei Funkwarnung (is-rf-warning) oder jeder Störung, die keine
    offene Zone ist. Details stehen in den Attributen.
    """

    _attr_has_entity_name = True
    _attr_name = "Wartung"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: SecvestCoordinator, entry: SecvestConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_maintenance"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
        )

    def _faults(self) -> list[dict[str, Any]]:
        return _maintenance_faults(self.coordinator.data.faults)

    @property
    def is_on(self) -> bool:
        return len(self._faults()) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        faults = self._faults()
        return {
            "meldungen": [f.get("ui-string", "?") for f in faults],
            "funkwarnung": any(f.get("is-rf-warning") for f in faults),
            "details": [
                {
                    "text": f.get("ui-string", "?"),
                    "typ": FAULT_TYPES.get(
                        str(f.get("type")), f"Code {f.get('type')}"
                    ),
                    "is_rf_warning": bool(f.get("is-rf-warning")),
                    "prevents_set": bool(f.get("prevents-set")),
                }
                for f in faults
            ],
        }
