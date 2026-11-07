"""Async API-Client für die ABUS Secvest (FUAA50000).

Auf Basis eines HAR-Mitschnitts der Web-UI verifiziert (07/2026).
Nur HTTP Basic Auth. Der frühere Form-Login-Fallback (/sec_login.cgi)
ist entfernt: Er hat als Web-Session einen gleichzeitig angemeldeten
Errichter aus der Web-Oberfläche geworfen bzw. ausgesperrt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .const import (
    DEFAULT_LOG_LIMIT,
    FAULT_TYPE_OPEN_ZONE,
    ENDPOINT_FAULTS,
    ENDPOINT_FORM_LOGIN,
    ENDPOINT_LOGS,
    ENDPOINT_PARTITION_SET,
    ENDPOINT_PARTITION_ZONES,
    ENDPOINT_PARTITIONS,
    STATE_SET,
    STATE_UNSET,
    ZONE_STATE_OPEN,
)

_LOGGER = logging.getLogger(__name__)

# Der TLS-Handshake der Anlage dauert gemessen ~7 s (schwache Panel-CPU,
# ECDHE-RSA-CHACHA20/TLSv1.2). Connect-Timeout entsprechend großzügig.
TIMEOUT = httpx.Timeout(10.0, connect=20.0)

# Führendes "Z<nr> <typ> " aus dem fault-ui-string entfernen -> reiner
# Zonenname, z.B. "Z302 A OG Draht" -> "OG Draht".
_ZONE_NAME_RE = re.compile(r"^Z\d+\s+\S+\s+")


def _zone_name_from_ui(ui: str) -> str:
    """Zonenname aus dem ui-string einer offenen-Zone-Störung ableiten."""
    stripped = _ZONE_NAME_RE.sub("", ui or "").strip()
    return stripped or (ui or "").strip()


class SecvestError(Exception):
    """Basisfehler."""


class SecvestAuthError(SecvestError):
    """Anmeldung fehlgeschlagen."""


class SecvestConnectionError(SecvestError):
    """Anlage nicht erreichbar."""


@dataclass
class SecvestData:
    """Normalisierter Zustand der Anlage."""

    # partition_id -> {"name": str, "state": "set"/"unset", "zones": [str]}
    partitions: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Störungen aus /faults/ (inkl. prevents-set)
    faults: list[dict[str, Any]] = field(default_factory=list)
    # Zonen/Melder: zone_id -> {"name": str, "partition": str|None,
    # "open": bool}. Grundgerüst aus /system/partitions/, Offen-Status +
    # Namen aus type-5000-Störungen in /faults/.
    zones: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Neueste Ereignisse aus /logs/ (client-seitig begrenzt)
    logs: list[dict[str, Any]] = field(default_factory=list)


class SecvestClient:
    """Client für die Secvest-REST-Schnittstelle."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        verify_ssl: bool = False,
    ) -> None:
        self._base = f"https://{host}:{port}"
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password),
            verify=verify_ssl,
            timeout=TIMEOUT,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self._form_login_done = False
        # Zonennamen tauchen nur in offenen-Zone-Störungen auf; einmal
        # gesehen, für die Prozesslaufzeit merken (zone_id -> Name).
        self._zone_names: dict[str, str] = {}

    async def async_close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # Low-level mit Auth-Fallback                                        #
    # ------------------------------------------------------------------ #

    async def _form_login(self) -> None:
        """Session-Login wie die Web-UI: POST usr/pwd, Cookie im Client."""
        try:
            resp = await self._client.post(
                self._base + ENDPOINT_FORM_LOGIN,
                data={"usr": self._username, "pwd": self._password},
            )
        except httpx.HTTPError as err:
            raise SecvestConnectionError("Form-Login nicht erreichbar") from err
        if resp.status_code >= 400:
            raise SecvestAuthError(
                f"Form-Login abgelehnt (HTTP {resp.status_code})"
            )
        self._form_login_done = True
        _LOGGER.debug("Form-Login als Session-Fallback ausgeführt")

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        try:
            resp = await self._client.request(method, self._base + path, **kwargs)
        except httpx.ConnectError as err:
            raise SecvestConnectionError(
                f"Keine Verbindung zu {self._base}"
            ) from err
        except httpx.TimeoutException as err:
            raise SecvestConnectionError("Timeout") from err

        # KEIN Form-Login-Fallback mehr: Eine Web-Session der Integration
        # beendet bzw. blockiert die Sitzung eines gleichzeitig angemeldeten
        # Errichters/Admins (Secvest erlaubt nur eine). Basic Auth reicht
        # auf der FUAA50000; bei 401/403 also direkt Auth-Fehler melden.
        if resp.status_code in (401, 403):
            raise SecvestAuthError(
                "Anmeldung abgelehnt – Benutzer Code / Web-Passwort prüfen"
            )
        return resp

    # ------------------------------------------------------------------ #
    # High-level                                                         #
    # ------------------------------------------------------------------ #

    async def async_validate(self) -> None:
        """Für den Config Flow: erreichbar + Auth ok?"""
        resp = await self._request("GET", ENDPOINT_PARTITIONS)
        if resp.status_code >= 300:
            raise SecvestConnectionError(
                f"Unerwartete Antwort: HTTP {resp.status_code}"
            )
        try:
            resp.json()
        except ValueError as err:
            raise SecvestConnectionError(
                "Antwort ist kein JSON – Firmware abweichend?"
            ) from err

    async def async_get_data(self) -> SecvestData:
        """Teilbereiche, Störungen und offene Zonen abholen."""
        data = SecvestData()

        # Teilbereiche (Pflicht)
        resp = await self._request("GET", ENDPOINT_PARTITIONS)
        if resp.status_code >= 300:
            raise SecvestConnectionError(
                f"Partitionsstatus fehlgeschlagen: HTTP {resp.status_code}"
            )
        for part in resp.json():
            try:
                pid = int(part["id"])
            except (KeyError, TypeError, ValueError):
                continue
            data.partitions[pid] = {
                "name": part.get("name", f"Teilbereich {pid}"),
                "state": str(part.get("state", "")).lower(),
                "zones": part.get("zones", []),
            }

        # Störungen (optional – die Anlage liefert hier gelegentlich 404)
        try:
            resp = await self._request("GET", ENDPOINT_FAULTS)
            if resp.status_code < 300:
                faults = resp.json()
                data.faults = faults if isinstance(faults, list) else []
        except (SecvestConnectionError, ValueError):
            _LOGGER.debug("Faults nicht abrufbar, wird übersprungen")

        # Zonen ableiten. sec_global_status.cgx liefert die offenen Zonen
        # nur einer eingeloggten Web-UI-Session (Basic: <authorised> leer),
        # daher NICHT nutzbar. Stattdessen: Grundgerüst aus den Partitionen,
        # Offen-Status + Namen aus type-5000-Störungen (Basic-tauglich).
        data.zones = self._build_zones(data.partitions, data.faults)

        # Melder direkt je Teilbereich (Namen + Offen-Status). Ergänzt die
        # Ableitung aus /faults/: auf manchen Firmwares fehlt dort die
        # type-5000-Störung bei unscharfer Anlage.
        for pid, part in data.partitions.items():
            if not part.get("zones"):
                continue  # Teilbereich ohne Melder -> keine Extra-Abfrage
            try:
                zone_list = await self.async_get_partition_zones(pid)
            except (SecvestConnectionError, SecvestAuthError):
                _LOGGER.debug("Zonen von Teilbereich %s nicht abrufbar", pid)
                continue
            self._merge_partition_zones(data, pid, zone_list)

        # Ereignisprotokoll (optional). Im selben sequentiellen Poll-Zyklus,
        # keine Extra-Requests außerhalb des Coordinators.
        try:
            data.logs = await self.async_get_logs()
        except SecvestConnectionError:
            _LOGGER.debug("Logs nicht abrufbar, werden übersprungen")

        return data

    def _build_zones(
        self,
        partitions: dict[int, dict[str, Any]],
        faults: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Zonenliste aus Partitionen + type-5000-Störungen bauen.

        Grundgerüst (alle Zonen, geschlossen) aus /system/partitions/;
        offene Zonen + reale Namen aus den offenen-Zone-Störungen.
        """
        zones: dict[str, dict[str, Any]] = {}
        for pid, part in partitions.items():
            for zid in part.get("zones", []):
                zid = str(zid)
                zones[zid] = {
                    "name": self._zone_names.get(zid, f"Zone {zid}"),
                    "partition": str(pid),
                    "open": False,
                }
        for fault in faults:
            if str(fault.get("type")) != FAULT_TYPE_OPEN_ZONE:
                continue
            zid = fault.get("affects-zone")
            if not zid:
                continue
            zid = str(zid)
            name = _zone_name_from_ui(fault.get("ui-string", ""))
            if name:
                self._zone_names[zid] = name
            affects = fault.get("affects-partition") or []
            zone = zones.setdefault(
                zid, {"name": None, "partition": None, "open": False}
            )
            zone["open"] = True
            zone["name"] = self._zone_names.get(zid, zone.get("name") or f"Zone {zid}")
            if not zone.get("partition") and affects:
                zone["partition"] = str(affects[0])
        return zones

    async def async_get_partition_zones(self, partition: int) -> list[dict]:
        """Melder eines Teilbereichs (GET /system/partitions-{n}/zones/).

        Liefert [] bei 404/Fehlantwort – ältere Firmware ohne diesen
        Endpoint fällt so auf die /faults/-Ableitung zurück.
        """
        resp = await self._request(
            "GET", ENDPOINT_PARTITION_ZONES.format(p=partition)
        )
        if resp.status_code >= 300:
            return []
        try:
            payload = resp.json()
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        return [z for z in payload if isinstance(z, dict)]

    @staticmethod
    def _zone_id_of(zone: dict[str, Any]) -> str | None:
        for key in ("id", "zone-id", "zone_id", "zone", "number"):
            value = zone.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    def _merge_partition_zones(
        self, data: SecvestData, partition: int, zone_list: list[dict]
    ) -> None:
        """Ergebnis von /system/partitions-{n}/zones/ in data.zones mergen.

        Zuordnung über eine ID im Payload; fehlt sie, über die Reihenfolge
        der Zonen-IDs aus /system/partitions/ (nur bei gleicher Anzahl).
        Offen ist eine Zone, wenn der Endpoint ODER eine type-5000-Störung
        das meldet.
        """
        if not zone_list:
            return
        known_ids = [str(z) for z in data.partitions[partition].get("zones", [])]
        by_position = len(zone_list) == len(known_ids)
        for idx, zone in enumerate(zone_list):
            zid = self._zone_id_of(zone)
            if zid is None:
                if not by_position:
                    continue
                zid = known_ids[idx]
            name = zone.get("name")
            state = str(zone.get("state", "")).lower()
            entry = data.zones.setdefault(
                zid, {"name": None, "partition": str(partition), "open": False}
            )
            if isinstance(name, str) and name.strip():
                self._zone_names[zid] = name.strip()
                entry["name"] = name.strip()
            elif not entry.get("name"):
                entry["name"] = f"Zone {zid}"
            if not entry.get("partition"):
                entry["partition"] = str(partition)
            entry["open"] = bool(entry.get("open")) or state == ZONE_STATE_OPEN

    async def async_get_logs(self, limit: int = DEFAULT_LOG_LIMIT) -> list[dict]:
        """Neueste Ereignisse aus /logs/.

        Die Anlage ignoriert ?limit und liefert immer alle (~600 Einträge,
        neueste zuerst); daher client-seitig auf die neuesten kürzen.
        """
        resp = await self._request("GET", ENDPOINT_LOGS)
        if resp.status_code >= 300:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        return data[:limit]

    async def async_get_partition_state(self, partition: int) -> str | None:
        """Nur den Zustand eines Teilbereichs lesen (1 Request)."""
        resp = await self._request("GET", ENDPOINT_PARTITIONS)
        if resp.status_code >= 300:
            return None
        try:
            parts = resp.json()
        except ValueError:
            return None
        for part in parts if isinstance(parts, list) else []:
            if str(part.get("id")) == str(partition):
                return str(part.get("state", "")).lower()
        return None

    async def async_set_partition(self, partition: int, mode: str) -> dict:
        """Teilbereich schalten.

        mode: "set" | "unset" (verifiziert) | "partset" (unverifiziert).
        Rückgabe: der von der Anlage bestätigte neue Zustand.
        """
        if mode not in (STATE_SET, STATE_UNSET, "partset"):
            raise ValueError(f"Ungültiger Modus: {mode}")
        path = ENDPOINT_PARTITION_SET.format(p=partition)
        resp = await self._request("PUT", path, json={"state": mode})
        if resp.status_code >= 300:
            raise SecvestError(
                f"Schalten fehlgeschlagen: HTTP {resp.status_code} – "
                "mögliche Ursache: Störung mit prevents-set oder offene Zone"
            )
        try:
            return resp.json()
        except ValueError:
            return {}
