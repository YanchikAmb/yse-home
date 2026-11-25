from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StationCoordinator


class StationEntity(CoordinatorEntity[StationCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: StationCoordinator, entry_id: str, title: str, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=title,
            manufacturer="hassio.by",
            model="Яндекс Станция, своя оболочка",
        )

    @property
    def voice(self) -> dict:
        return self.coordinator.data.get("voice") or {}

    @property
    def device(self) -> dict:
        return self.coordinator.data.get("device") or {}
