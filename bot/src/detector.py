"""Чисте правило виявлення переходу стану мережі."""
from __future__ import annotations

from typing import Literal


BatteryDirection = Literal["charging", "discharging", "idle", "unknown"]
BatteryAlert = tuple[Literal["charging", "discharging"], int]

BATTERY_POWER_DEADBAND_WATTS = 10.0
DISCHARGE_THRESHOLDS = (90, 50, 25)
CHARGE_THRESHOLDS = (90, 99)


def detect_transition(previous: bool | None, current: bool) -> str | None:
    """Повертає 'gridLost'/'gridRestored' при зміні стану, інакше None.

    Перший знімок (previous is None) — базова лінія, не подія.
    """
    if previous is None:
        return None
    if previous == current:
        return None
    return "gridRestored" if current else "gridLost"


def battery_direction(power_watts: float | None) -> BatteryDirection:
    """Визначає напрям потоку: у FSolar від'ємна потужність означає заряджання."""
    if power_watts is None:
        return "unknown"
    if power_watts < -BATTERY_POWER_DEADBAND_WATTS:
        return "charging"
    if power_watts > BATTERY_POWER_DEADBAND_WATTS:
        return "discharging"
    return "idle"


def detect_battery_thresholds(
    previous_soc: int | None,
    current_soc: int,
    direction: BatteryDirection,
) -> list[BatteryAlert]:
    """Повертає пороги SOC, які перетнуто у правильному напрямку з минулого знімка."""
    if previous_soc is None:
        return []

    if direction == "discharging":
        return [
            ("discharging", threshold)
            for threshold in DISCHARGE_THRESHOLDS
            if previous_soc >= threshold and current_soc < threshold
        ]

    if direction == "charging":
        return [
            ("charging", threshold)
            for threshold in CHARGE_THRESHOLDS
            if previous_soc <= threshold and current_soc > threshold
        ]

    return []
