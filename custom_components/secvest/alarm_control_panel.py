"""Alarm-Panel-Entities: eine pro Secvest-Teilbereich.

Nutzt die echten Namen aus der Anlage (z.B. "Haustür", "EG", "OG").
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SecvestConfigEntry, enabled_partitions
from .api import SecvestError
from .const import (
    ARM_VERIFY_ATTEMPTS,
    ARM_VERIFY_INTERVAL,
    CONF_DISARM_CODE,
    DOMAIN,
    STATE_ACKNOWLEDGED,
    STATE_ALARM,
    STATE_PARTSET,
    STATE_SET,
    STATE_UNSET,
)
from .coordinator import SecvestCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SecvestConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    enabled = enabled_partitions(entry, coordinator)
    async_add_entities(
        SecvestAlarmPanel(coordinator, entry, pid)
        for pid in sorted(coordinator.data.partitions)
        if pid in enabled
    )


class SecvestAlarmPanel(CoordinatorEntity[SecvestCoordinator], AlarmControlPanelEntity):
    """Ein Teilbereich der Secvest als Alarmpanel."""

    _attr_has_entity_name = True
    # ARM_HOME = "partset" (intern scharf); auf TB1 verifiziert. Nicht alle
    # Teilbereiche unterstützen es – nicht-fähige antworten mit HTTP 409,
    # was async_alarm_arm_home als HomeAssistantError sichtbar macht.
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_HOME
    )
    _attr_code_arm_required = False

    def __init__(
        self,
        coordinator: SecvestCoordinator,
        entry: SecvestConfigEntry,
        partition: int,
    ) -> None:
        super().__init__(coordinator)
        self._partition = partition
        # Übergangszustand (arming/disarming) für sofortiges UI-Feedback,
        # solange Schalten + Nachprüfung laufen.
        self._pending: AlarmControlPanelState | None = None
        self._disarm_code: str = str(
            entry.options.get(CONF_DISARM_CODE) or ""
        ).strip()
        self._attr_unique_id = f"{entry.entry_id}_partition_{partition}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="ABUS Secvest",
            manufacturer="ABUS",
            model="Secvest FUAA50000",
        )

    @property
    def code_format(self) -> CodeFormat | None:
        """Codefeld nur zeigen, wenn ein Unscharf-Code gesetzt ist."""
        if not self._disarm_code:
            return None
        return CodeFormat.NUMBER if self._disarm_code.isdigit() else CodeFormat.TEXT

    def _data(self) -> dict[str, Any] | None:
        return self.coordinator.data.partitions.get(self._partition)

    @property
    def name(self) -> str:
        part = self._data()
        return part["name"] if part else f"Teilbereich {self._partition}"

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        if self._pending is not None:
            return self._pending
        part = self._data()
        if not part:
            return None
        state = part["state"]
        # Alarm zuerst prüfen: "set-alarm" (Vollalarm) und "acknowledged"
        # (quittiert, noch nicht zurückgesetzt) -> TRIGGERED. "alarm" in
        # state fängt zusätzlich mögliche Varianten (z.B. part-alarm) ab.
        if state in (STATE_ALARM, STATE_ACKNOWLEDGED) or "alarm" in state:
            return AlarmControlPanelState.TRIGGERED
        if state == STATE_UNSET:
            return AlarmControlPanelState.DISARMED
        if state == STATE_SET:
            return AlarmControlPanelState.ARMED_AWAY
        if state == STATE_PARTSET:
            return AlarmControlPanelState.ARMED_HOME
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Störungen und offene Zonen dieses Teilbereichs anzeigen."""
        pid = str(self._partition)
        faults = [
            f.get("ui-string", "?")
            for f in self.coordinator.data.faults
            if pid in f.get("affects-partition", [])
        ]
        prevents_set = any(
            f.get("prevents-set")
            for f in self.coordinator.data.faults
            if pid in f.get("affects-partition", [])
        )
        open_zones = [
            z["name"]
            for z in self.coordinator.data.zones.values()
            if z.get("open") and z.get("partition") == pid
        ]
        part = self._data()
        return {
            "panel_state": part["state"] if part else None,
            "faults": faults,
            "prevents_set": prevents_set,
            "open_zones": open_zones,
        }

    def _set_pending(self, state: AlarmControlPanelState | None) -> None:
        self._pending = state
        self.async_write_ha_state()

    def _apply_state(self, state: str | None) -> None:
        """Bestätigten Anlagenzustand sofort übernehmen (nicht auf Poll warten)."""
        part = self._data()
        if state and part is not None:
            part["state"] = state
            self.coordinator.async_update_listeners()

    def _open_zone_names(self) -> list[str]:
        pid = str(self._partition)
        return [
            z.get("name") or f"Zone {zid}"
            for zid, z in self.coordinator.data.zones.items()
            if z.get("open") and z.get("partition") == pid
        ]

    async def _switch(self, mode: str) -> None:
        try:
            await self.coordinator.client.async_set_partition(
                self._partition, mode
            )
        except SecvestError as err:
            raise HomeAssistantError(str(err)) from err

    async def _arm(self, mode: str) -> None:
        """Scharfschalten mit Vor- und Nachprüfung.

        Vorher: offene Zonen -> verständlicher Fehler statt stillem
        Abbruch. Nachher: einige Sekunden prüfen, ob die Anlage den
        Teilbereich wirklich nicht mehr als unscharf meldet; manche
        Firmwares quittieren den PUT mit 2xx und brechen intern ab.
        """
        open_zones = self._open_zone_names()
        if open_zones:
            raise HomeAssistantError(
                f"{self.name} nicht scharf geschaltet – offen: "
                + ", ".join(open_zones)
            )
        self._set_pending(AlarmControlPanelState.ARMING)
        state: str | None = None
        try:
            await self._switch(mode)
            for _ in range(ARM_VERIFY_ATTEMPTS):
                await asyncio.sleep(ARM_VERIFY_INTERVAL)
                try:
                    state = await self.coordinator.client.async_get_partition_state(
                        self._partition
                    )
                except SecvestError:
                    state = None
                if state and state != STATE_UNSET:
                    break
            self._apply_state(state)
        finally:
            self._set_pending(None)
        if state == STATE_UNSET:
            raise HomeAssistantError(
                f"{self.name}: Secvest hat das Scharfschalten nicht bestätigt "
                "– bitte am Bedienteil prüfen"
            )

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._arm(STATE_SET)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        if self._disarm_code and not hmac.compare_digest(
            str(code or ""), self._disarm_code
        ):
            raise HomeAssistantError("Falscher Code – nicht unscharf geschaltet")
        self._set_pending(AlarmControlPanelState.DISARMING)
        try:
            await self._disarm_with_followup()
        finally:
            self._set_pending(None)

    async def _disarm_with_followup(self) -> None:
        await self._switch(STATE_UNSET)
        state: str | None = None
        # Bei laufendem Alarm wertet die Secvest das erste "unset" nur als
        # Quittieren (set-alarm -> acknowledged); erst ein zweites "unset"
        # schaltet wirklich unscharf. Daher bis zu zweimal nachlegen.
        for _ in range(2):
            await asyncio.sleep(ARM_VERIFY_INTERVAL)
            try:
                state = await self.coordinator.client.async_get_partition_state(
                    self._partition
                )
            except SecvestError:
                break
            if state is None or state == STATE_UNSET:
                self._apply_state(state)
                break
            if state == STATE_ACKNOWLEDGED or "alarm" in state:
                _LOGGER.debug(
                    "Teilbereich %s nach Unscharf noch '%s' – erneut unscharf",
                    self._partition,
                    state,
                )
                await self._switch(STATE_UNSET)
            else:
                break
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Intern scharf (partset). Nur auf dafür konfigurierten
        Teilbereichen erlaubt; sonst antwortet die Anlage mit HTTP 409."""
        await self._arm(STATE_PARTSET)
