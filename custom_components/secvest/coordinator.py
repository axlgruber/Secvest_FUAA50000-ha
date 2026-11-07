"""Polling-Coordinator für die Secvest."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SecvestAuthError, SecvestClient, SecvestConnectionError, SecvestData
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_TOLERATED_FAILURES,
)

_LOGGER = logging.getLogger(__name__)


class SecvestCoordinator(DataUpdateCoordinator[SecvestData]):
    """Fragt die Anlage zyklisch ab (die Secvest kennt kein Push)."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: SecvestClient
    ) -> None:
        interval = int(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self._failures = 0

    async def _async_update_data(self) -> SecvestData:
        try:
            data = await self.client.async_get_data()
        except SecvestAuthError as err:
            # Auth sofort melden (z.B. Errichter angemeldet) – kein Retry,
            # wiederholte Fehl-Logins können Sabotage auslösen.
            raise ConfigEntryAuthFailed(err) from err
        except SecvestConnectionError as err:
            self._failures += 1
            if self.data is not None and self._failures <= MAX_TOLERATED_FAILURES:
                _LOGGER.debug(
                    "Secvest-Abfrage fehlgeschlagen (%s/%s), behalte letzte Daten: %s",
                    self._failures,
                    MAX_TOLERATED_FAILURES,
                    err,
                )
                return self.data
            raise UpdateFailed(err) from err
        self._failures = 0
        return data
