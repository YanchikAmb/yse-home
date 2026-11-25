from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import LIGHT_LUX, EntityCategory, UnitOfPower, UnitOfTemperature
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN
from .entity import StationEntity
from .people import SIGNAL_SPEAKER


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        HeardSensor(coordinator, entry.entry_id, entry.title, "heard"),
        WifiSensor(coordinator, entry.entry_id, entry.title, "wifi"),
        AlarmSensor(coordinator, entry.entry_id, entry.title, "alarm"),
        SpeakerSensor(coordinator, entry.entry_id, entry.title, "speaker"),
        LuxSensor(coordinator, entry.entry_id, entry.title, "lux"),
        ChipSensor(coordinator, entry.entry_id, entry.title, "chip"),
        PowerSensor(coordinator, entry.entry_id, entry.title, "watts"),
        HeadphoneSensor(coordinator, entry.entry_id, entry.title, "headphones"),
        BleCountSensor(coordinator, entry.entry_id, entry.title, "ble_count"),
        BleDevicesSensor(coordinator, entry.entry_id, entry.title, "ble_devices"),
    ])


class SpeakerSensor(StationEntity, SensorEntity):
    """Кто последним говорил с колонкой (по отпечатку голоса на сервере)."""

    def __init__(self, coordinator, entry_id, title, key):
        super().__init__(coordinator, entry_id, title, key)
        self._entry_id = entry_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, f"{SIGNAL_SPEAKER}_{self._entry_id}", self.async_write_ha_state))

    @property
    def _state(self) -> dict:
        return self.hass.data.get(DOMAIN + "_speaker", {}).get(self._entry_id) or {}

    @property
    def native_value(self):
        return self._state.get("name") or None

    @property
    def extra_state_attributes(self):
        state = self._state
        if not state:
            return None
        return {
            "person": state.get("person") or None,
            "score": round(state.get("score", 0), 2),
            "heard_at": datetime.fromtimestamp(state["at"], tz=timezone.utc).isoformat(),
        }


class HeardSensor(StationEntity, SensorEntity):
    @property
    def native_value(self):
        return (self.voice.get("heard") or "")[:250] or None


class WifiSensor(StationEntity, SensorEntity):
    """SSID Wi‑Fi или Ethernet — обычный сенсор, не diagnostic, чтобы сразу был виден в UI."""

    @property
    def native_value(self):
        return self.device.get("wifi_now") or None


class LuxSensor(StationEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_suggested_display_precision = 2

    @property
    def available(self):
        return super().available and self.device.get("lux") is not None

    @property
    def native_value(self):
        return self.device.get("lux")


class ChipSensor(StationEntity, SensorEntity):
    """Нагрев SoC колонки (°C), не температура комнаты. Без device_class temperature —
    иначе Assist путает с комнатной температурой из climate."""

    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    @property
    def available(self):
        return super().available and self.device.get("chip_c") is not None

    @property
    def native_value(self):
        return self.device.get("chip_c")


class PowerSensor(StationEntity, SensorEntity):
    """Ватты по USB-C. Есть у Станции 2, у Мини 2 этого контроллера нет."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    @property
    def available(self):
        return super().available and self.device.get("watts") is not None

    @property
    def native_value(self):
        return self.device.get("watts")

    @property
    def extra_state_attributes(self):
        if self.device.get("volts") is None:
            return None
        return {"voltage": self.device.get("volts"), "current": self.device.get("amps")}


class HeadphoneSensor(StationEntity, SensorEntity):
    @property
    def available(self):
        return super().available and self.device.get("headphones") is not None

    @property
    def native_value(self):
        flag = self.device.get("headphones")
        if flag is None:
            return None
        return "да" if flag else "нет"


class AlarmSensor(StationEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        for row in self.device.get("alarms") or []:
            try:
                at = int(row["at"])
            except (KeyError, TypeError, ValueError):
                continue
            if at > 0:
                return datetime.fromtimestamp(at, tz=timezone.utc)
        return None

    @property
    def extra_state_attributes(self):
        return {
            "alarms": [
                {key: row.get(key) for key in ("time", "repeat", "on", "label", "when")}
                for row in self.device.get("alarms") or []
                if isinstance(row, dict)
            ]
        }


class BleCountSensor(StationEntity, SensorEntity):
    """Сколько BLE-устройств прямо сейчас видит колонка (пассивный шлюз)."""

    @property
    def native_value(self):
        ble = self.coordinator.data.get("ble") or {}
        if "total" in ble:
            return ble.get("total")
        return self.device.get("ble_count")


class BleDevicesSensor(StationEntity, SensorEntity):
    """Сводка BLE: датчики BTHome/Xiaomi/ATC и ближайшие устройства по RSSI."""

    @property
    def native_value(self):
        ble = self.coordinator.data.get("ble") or {}
        sensors = ble.get("sensors")
        if sensors is None:
            return None
        return f"{sensors} датч. / {ble.get('total', 0)} устр."

    @property
    def extra_state_attributes(self):
        ble = self.coordinator.data.get("ble") or {}
        if not ble:
            return None
        return {
            "scan_note": ble.get("scan_note"),
            "mqtt_note": ble.get("mqtt_note"),
            "total": ble.get("total"),
            "sensors": ble.get("sensors"),
            "devices": ble.get("devices") or [],
        }
