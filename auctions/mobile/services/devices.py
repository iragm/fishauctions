import logging
import uuid as uuid_module

from auctions.models import MobileDevice

logger = logging.getLogger(__name__)


class DeviceService:
    """Manages mobile device registration and updates."""

    @staticmethod
    def register_or_update(
        user,
        device_uuid: str | uuid_module.UUID,
        device_name: str = "",
        platform: str = "",
        app_version: str = "",
    ) -> tuple[MobileDevice, bool]:
        """Create or update a device record for the given user.

        Returns (device, created) — mirrors QuerySet.update_or_create semantics.
        If device_uuid is already registered to a different user, the record is
        re-assigned to the current user (handles device re-use after factory reset).
        """
        if not isinstance(device_uuid, uuid_module.UUID):
            try:
                device_uuid = uuid_module.UUID(str(device_uuid))
            except (ValueError, AttributeError):
                msg = f"Invalid device_uuid: {device_uuid!r}"
                raise ValueError(msg)

        device, created = MobileDevice.objects.update_or_create(
            device_uuid=device_uuid,
            defaults={
                "user": user,
                "device_name": device_name,
                "platform": platform,
                "app_version": app_version,
            },
        )
        action = "registered" if created else "updated"
        logger.info("Mobile device %s for user %s: %s", action, user.pk, device_uuid)
        return device, created
