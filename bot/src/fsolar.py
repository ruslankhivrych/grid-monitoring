"""Клієнт приватного FSolar API — порт з docs/fsolar-api.md та verify-api.ps1.

Логін: пароль шифрується RSA(PKCS#1) публічним ключем FSolar → base64.
Токен має префікс `Bearer_` і йде в заголовок Authorization дослівно.
Конверт відповідей: {code, message, data}; code 998 = токен протух → релогін.
"""
from __future__ import annotations

import base64
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

BASE_URL = "https://shine-api.felicitysolar.com"

# Публічний RSA-ключ FSolar (SPKI, base64), отриманий з веб-клієнта FSolar.
PUBLIC_KEY_SPKI = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnAJE68pjWZmtSg6ZJs9FZugJXC6bBSluTW6mJtt"
    "OLOaljrdErVnM5DNN+YFzpB9pAysTErjY1bnSVuEwQSwptnqUji7Ch2qMj2n+0eCp8p6vtSh7/tFr2ul8nD"
    "RtkoswLANAIwtUk/G85ipMpmY1W642LImnEJmGkkddlbjbjxJTZWR5hc/d9cPWb+AR77LxFFrMik3c+44v1"
    "kQlIPFP6EjIbOvt/Lv7fHWD9JI/YzN4y1gK7C/VQdNGuikQyNg+5W3rg9ecYf9I5uLAQwY/hxeI3lbNsEre"
    "bqKe2EbJ8AwcNIC0lDBz53Sq0ML89QapEuy3fB+upuctxLULVDCbNwIDAQAB"
)

MAINS_VOLTAGE_THRESHOLD = 50.0


class FSolarError(Exception):
    pass


class _TokenExpired(Exception):
    """Внутрішня: code == 998."""


@dataclass
class Snapshot:
    is_present: bool
    voltage: float | None
    frequency: float | None
    work_mode: str
    soc: int | None
    batt_voltage: float | None
    batt_current: float | None
    timestamp: datetime | None
    batt_power: float | None = None      # emsPowerTotal/emsPower, Вт: + заряд, - розряд
    load_percent: int | None = None      # loadPercent, %
    capacity: int | None = None          # totalEmsCapacity
    load_power: float | None = None      # totalConsumPower, Вт
    load_energy_today: float | None = None   # eLoadToday, кВт·год
    load_energy_month: float | None = None   # eLoadMonth, кВт·год
    load_energy_year: float | None = None    # eLoadYear, кВт·год
    load_energy_total: float | None = None   # eLoadTotal, кВт·год

    @property
    def grid_known(self) -> bool:
        return self.voltage is not None or bool(self.work_mode)


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _first_present(*values):
    """Повертає перше значення, яке не є None або порожнім рядком."""
    return next((value for value in values if value is not None and value != ""), None)


def _source_timezone(value) -> tzinfo | None:
    """FSolar timeZone is usually UTC+03:00; also accept IANA zone names."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value in {"UTC", "GMT", "Z"}:
        return timezone.utc
    match = re.fullmatch(r"(?:UTC|GMT)?([+-])(\d{1,2}):?(\d{2})", value)
    if match:
        sign, hours, minutes = match.groups()
        if int(hours) > 23 or int(minutes) > 59:
            return None
        offset = timedelta(hours=int(hours), minutes=int(minutes))
        return timezone(offset if sign == "+" else -offset)
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _snapshot_timestamp(data: dict, device_timezone: tzinfo) -> datetime | None:
    # The FSolar web UI displays dataTimeStr together with timeZone, without
    # converting dataTime. Prefer those same source fields and store UTC once.
    raw_text = data.get("dataTimeStr")
    if isinstance(raw_text, str) and re.match(
        r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", raw_text.strip()
    ):
        try:
            local_time = datetime.fromisoformat(raw_text.strip())
        except ValueError:
            pass
        else:
            if local_time.tzinfo is None:
                source_timezone = _source_timezone(data.get("timeZone"))
                if data.get("timeZone") and source_timezone is None:
                    # Do not silently misinterpret an explicit but unknown zone.
                    return None
                local_time = local_time.replace(tzinfo=source_timezone or device_timezone)
            try:
                return local_time.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                return None

    # Compatibility with responses containing only a millisecond timestamp.
    milliseconds = _to_float(data.get("dataTime"))
    if milliseconds is None or not math.isfinite(milliseconds) or milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def parse_snapshot(data: dict, *, device_timezone: tzinfo = timezone.utc) -> Snapshot:
    """Мапінг відповіді get_device_snapshot у стан мережі/батареї (тестоване)."""
    voltage = _to_float(data.get("acRInVolt"))
    frequency = _to_float(data.get("acRInFreq"))
    work_mode = (data.get("workModeStr") or "").strip()

    # Стан мережі: напруга на вході > порога АБО режим "Line".
    is_present = (voltage or 0) > MAINS_VOLTAGE_THRESHOLD or "line" in work_mode.lower()

    soc_raw = _first_present(data.get("emsSoc"), data.get("emsSocAvg"), data.get("battSoc"))
    soc_f = _to_float(soc_raw)
    soc = int(soc_f) if soc_f is not None else None

    timestamp = _snapshot_timestamp(data, device_timezone)

    load_f = _to_float(data.get("loadPercent"))
    cap_f = _to_float(data.get("totalEmsCapacity"))
    batt_voltage = _to_float(data.get("emsVoltage"))
    batt_current = _to_float(data.get("emsCurrent"))
    batt_power = _to_float(_first_present(data.get("emsPowerTotal"), data.get("emsPower")))
    if batt_power is None and batt_voltage is not None and batt_current is not None:
        batt_power = batt_voltage * batt_current

    return Snapshot(
        is_present=is_present,
        voltage=voltage,
        frequency=frequency,
        work_mode=work_mode,
        soc=soc,
        batt_voltage=batt_voltage,
        batt_current=batt_current,
        timestamp=timestamp,
        batt_power=batt_power,
        load_percent=int(load_f) if load_f is not None else None,
        capacity=int(cap_f) if cap_f is not None else None,
        load_power=_to_float(
            _first_present(
                data.get("totalConsumPower"),
                data.get("loadPower"),
                data.get("loadPowerConvertion"),
                data.get("acTotalOutActPower"),
            )
        ),
        load_energy_today=_to_float(
            _first_present(data.get("eLoadToday"), data.get("tLoadToday"))
        ),
        load_energy_month=_to_float(
            _first_present(data.get("eLoadMonth"), data.get("tLoadMonth"))
        ),
        load_energy_year=_to_float(
            _first_present(data.get("eLoadYear"), data.get("tLoadYear"))
        ),
        load_energy_total=_to_float(
            _first_present(data.get("eLoadTotal"), data.get("tLoadTotal"))
        ),
    )


def encrypt_password(plain: str, spki_b64: str = PUBLIC_KEY_SPKI) -> str:
    key = load_der_public_key(base64.b64decode(spki_b64))
    cipher = key.encrypt(plain.encode("utf-8"), padding.PKCS1v15())  # type: ignore[arg-type]
    return base64.b64encode(cipher).decode("ascii")


class FSolarClient:
    def __init__(
        self,
        username: str,
        password: str,
        device_sn: str,
        device_type: str = "OG",
        *,
        device_timezone: tzinfo = timezone.utc,
    ):
        self.username = username
        self.password = password
        self.device_sn = device_sn
        self.device_type = device_type
        self.device_timezone = device_timezone
        self._token: str | None = None
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=20)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def login(self) -> None:
        body = {
            "userName": self.username,
            "password": encrypt_password(self.password),
            "version": "1.0",
        }
        data = await self._raw_post("/userlogin", body, auth=False)
        token = (data or {}).get("token")
        if not token:
            raise FSolarError("Логін не повернув токен")
        self._token = token

    async def get_snapshot(self) -> Snapshot:
        body = {
            "deviceSn": self.device_sn,
            "deviceType": self.device_type,
            "dateStr": datetime.now(self.device_timezone).strftime("%Y-%m-%d %H:%M:%S"),
        }
        data = await self._post("/device/get_device_snapshot", body)
        return parse_snapshot(data or {}, device_timezone=self.device_timezone)

    async def get_alarms(self, days: int = 7) -> list[dict]:
        now_ms = int(time.time() * 1000)
        from_ms = now_ms - days * 86_400 * 1000
        body = {
            "pageNum": 1, "pageSize": 50, "plantName": "", "deviceSn": self.device_sn,
            "status": "", "warringType": "", "userName": "", "orgCode": "",
            "faultcode": "", "deviceModel": "", "deviceAlias": "",
            "leftDate": from_ms, "rightDate": now_ms,
        }
        data = await self._post("/device/device_warring_list", body)
        return (data or {}).get("dataList") or []

    # --- internals ---

    async def _post(self, path: str, body: dict) -> dict | None:
        if not self._token:
            await self.login()
        try:
            return await self._raw_post(path, body)
        except _TokenExpired:
            await self.login()
            return await self._raw_post(path, body)

    async def _raw_post(self, path: str, body: dict, auth: bool = True) -> dict | None:
        headers = {"lang": "en_US", "source": "WEB"}
        if auth and self._token:
            headers["Authorization"] = self._token
        response = await self._client.post(path, json=body, headers=headers)
        response.raise_for_status()
        envelope = response.json()
        code = envelope.get("code")
        if code == 200:
            return envelope.get("data")
        if code == 998:
            raise _TokenExpired()
        raise FSolarError(f"FSolar code={code}: {envelope.get('message')}")
