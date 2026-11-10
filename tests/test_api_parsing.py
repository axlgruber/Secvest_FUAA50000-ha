"""Reine Parser-Tests für api.py – ohne Home Assistant.

Lädt api.py/const.py direkt (die __init__.py der Integration importiert
HA) und mockt die HTTP-Antworten mit httpx.MockTransport. Läuft mit
plain pytest + httpx, auch ohne installiertes Home Assistant.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx

# --- api.py + const.py standalone laden ------------------------------- #
_PKG = Path(__file__).resolve().parent.parent / "custom_components" / "secvest"
_pkg = types.ModuleType("secvest_standalone")
_pkg.__path__ = [str(_PKG)]
sys.modules["secvest_standalone"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"secvest_standalone.{name}", _PKG / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"secvest_standalone.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


const = _load("const")
api = _load("api")

# --- Beispiel-Payloads (KEINE echten Zugangsdaten) -------------------- #
PARTITIONS = [
    {"id": "1", "name": "Haustür", "state": "unset", "zones": ["303"]},
    {"id": "2", "name": "EG", "state": "set", "zones": ["301"]},
    {"id": "3", "name": "OG", "state": "set-alarm", "zones": ["302"]},
    {"id": "4", "name": "Teilber. 4", "state": "unset", "zones": []},
]
FAULT_OPEN_ZONE = {
    "type": "5000",
    "id": "1555",
    "ui-string": "Z302 A OG Draht",
    "affects-partition": ["3"],
    "affects-zone": "302",
    "prevents-set": True,
    "prevents-reset": False,
    "is-rf-warning": False,
}
LOGS = [
    {
        "id": str(1000 - i),
        "type": "normal",
        "desc": f"Ereignis {i}",
        "events": [{"timestamp": str(1783418800 - i), "username": "Tester"}],
    }
    for i in range(20)
]


def _make_client(faults=None, logs=None):
    handler_faults = [FAULT_OPEN_ZONE] if faults is None else faults
    handler_logs = LOGS if logs is None else logs

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == const.ENDPOINT_PARTITIONS:
            return httpx.Response(200, json=PARTITIONS)
        if path == const.ENDPOINT_FAULTS:
            return httpx.Response(200, json=handler_faults)
        if path == const.ENDPOINT_LOGS:
            return httpx.Response(200, json=handler_logs)
        return httpx.Response(404, text="not found")

    client = api.SecvestClient("host", 4433, "user", "pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _run(coro):
    return asyncio.run(coro)


# --- Tests ------------------------------------------------------------ #
def test_zone_name_from_ui():
    assert api._zone_name_from_ui("Z302 A OG Draht") == "OG Draht"
    assert api._zone_name_from_ui("Z301 A EG Draht") == "EG Draht"
    # Ohne Präfix bleibt der String erhalten
    assert api._zone_name_from_ui("Sabotage Zentrale") == "Sabotage Zentrale"
    assert api._zone_name_from_ui("") == ""


def test_partitions_parsed():
    client = _make_client()
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert set(data.partitions) == {1, 2, 3, 4}
    assert data.partitions[1]["name"] == "Haustür"
    assert data.partitions[2]["state"] == "set"
    assert data.partitions[3]["state"] == "set-alarm"
    assert data.partitions[4]["zones"] == []


def test_zones_from_faults():
    client = _make_client(faults=[FAULT_OPEN_ZONE])
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    # Zone 302 offen, Name aus fault-ui-string, Teilbereich 3
    assert data.zones["302"]["open"] is True
    assert data.zones["302"]["name"] == "OG Draht"
    assert data.zones["302"]["partition"] == "3"
    # geschlossene Zonen: generischer Name, nicht offen
    assert data.zones["301"]["open"] is False
    assert data.zones["301"]["name"] == "Zone 301"
    assert data.zones["303"]["open"] is False


def test_zones_all_closed_when_no_faults():
    client = _make_client(faults=[])
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert all(not z["open"] for z in data.zones.values())
    assert set(data.zones) == {"301", "302", "303"}


def test_logs_limited():
    client = _make_client()
    try:
        logs = _run(client.async_get_logs())
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert len(logs) == const.DEFAULT_LOG_LIMIT
    assert logs[0]["desc"] == "Ereignis 0"
    assert len(data.logs) == const.DEFAULT_LOG_LIMIT


def test_open_zone_endpoint_gone():
    # sec_global_status wird nicht mehr abgefragt -> auch ohne diesen
    # Endpoint liefert async_get_data ein vollständiges Ergebnis.
    client = _make_client()
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert data.zones  # nicht leer


# --- /system/partitions-{n}/zones/ ------------------------------------ #
def _make_zone_client(zone_payloads, faults=None):
    """Client, der zusätzlich Melder je Teilbereich liefert."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == const.ENDPOINT_PARTITIONS:
            return httpx.Response(200, json=PARTITIONS)
        if path == const.ENDPOINT_FAULTS:
            return httpx.Response(200, json=faults or [])
        if path == const.ENDPOINT_LOGS:
            return httpx.Response(200, json=[])
        for pid, payload in zone_payloads.items():
            if path == const.ENDPOINT_PARTITION_ZONES.format(p=pid):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, text="not found")

    client = api.SecvestClient("host", 4433, "user", "pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_partition_zones_open_without_fault():
    # Anlage unscharf, Tür offen, aber KEINE type-5000-Störung.
    client = _make_zone_client(
        {
            2: [{"name": "Terrassentür", "state": "open"}],
            1: [{"name": "Haustür", "state": "closed"}],
        }
    )
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert data.zones["301"]["open"] is True
    assert data.zones["301"]["name"] == "Terrassentür"
    assert data.zones["303"]["open"] is False
    assert data.zones["303"]["name"] == "Haustür"
    # Teilbereich 3 ohne Zonen-Endpoint (404) -> Fallback bleibt
    assert data.zones["302"]["name"] == "Zone 302"


def test_partition_zones_matched_by_id():
    client = _make_zone_client(
        {2: [{"id": "301", "name": "EG Draht", "state": "closed"}]}
    )
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert data.zones["301"]["name"] == "EG Draht"
    assert data.zones["301"]["open"] is False


def test_partition_zones_fault_still_counts():
    # Endpoint sagt zu, Störung sagt offen -> offen (ODER-Verknüpfung).
    client = _make_zone_client(
        {3: [{"name": "OG Draht", "state": "closed"}]}, faults=[FAULT_OPEN_ZONE]
    )
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert data.zones["302"]["open"] is True


def test_partition_zones_position_mismatch_ignored():
    # Zwei Einträge ohne ID für einen Teilbereich mit einer Zone -> nicht
    # raten, generischer Name bleibt.
    client = _make_zone_client(
        {2: [{"name": "A", "state": "open"}, {"name": "B", "state": "open"}]}
    )
    try:
        data = _run(client.async_get_data())
    finally:
        _run(client.async_close())
    assert data.zones["301"]["name"] == "Zone 301"
    assert data.zones["301"]["open"] is False



def test_request_retries_once_on_dropped_connection():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(200, json=PARTITIONS)

    client = api.SecvestClient("host", 4433, "user", "pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        state = _run(client.async_get_partition_state(2))
    finally:
        _run(client.async_close())
    assert state == "set"
    assert calls["n"] == 2


def test_set_partition_returns_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "2", "name": "EG", "state": "set"})

    client = api.SecvestClient("host", 4433, "user", "pw")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        reply = _run(client.async_set_partition(2, "set"))
    finally:
        _run(client.async_close())
    assert reply["state"] == "set"


def test_get_data_without_logs_keeps_last_logs():
    client = _make_client()
    seen = []
    orig = client._client

    try:
        first = _run(client.async_get_data())
        second = _run(client.async_get_data(include_logs=False))
    finally:
        _run(client.async_close())
    assert len(first.logs) == const.DEFAULT_LOG_LIMIT
    assert second.logs == first.logs
