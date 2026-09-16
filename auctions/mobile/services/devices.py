import logging
import uuid as uuid_module

from django.db import transaction
from django.utils import timezone

from auctions.models import MobileDevice

logger = logging.getLogger(__name__)


class DeviceService:
    """Manages mobile device registration and updates."""

    @staticmethod
    def _coerce_uuid(device_uuid):
        if isinstance(device_uuid, uuid_module.UUID):
            return device_uuid
        try:
            return uuid_module.UUID(str(device_uuid))
        except (ValueError, AttributeError):
            msg = f"Invalid device_uuid: {device_uuid!r}"
            raise ValueError(msg)

    @staticmethod
    @transaction.atomic
    def register_or_update(
        user,
        device_uuid: str | uuid_module.UUID,
        device_name: str = "",
        platform: str = "",
        app_version: str = "",
        fcm_token: str | None = None,
    ) -> tuple[MobileDevice, bool]:
        """Create or update a device record for the given user; returns ``(device, created)``.

        A device_uuid registered to a different user is re-assigned to this one, which is what happens after
        a factory reset. ``fcm_token`` is upserted when given (``None`` leaves an existing token alone), and
        a non-empty token seen here is cleared from any *other* device row still holding it: FCM tokens
        follow the app install rather than the user, so on a shared device a stale row would otherwise push
        to the wrong account.
        """
        device_uuid = DeviceService._coerce_uuid(device_uuid)

        defaults = {
            "user": user,
            "device_name": device_name,
            "platform": platform,
            "app_version": app_version,
        }
        if fcm_token is not None:
            defaults["fcm_token"] = fcm_token
            defaults["fcm_token_updated_at"] = timezone.now()

        device, created = MobileDevice.objects.update_or_create(
            device_uuid=device_uuid,
            defaults=defaults,
        )

        if fcm_token:
            # The same physical token must live on exactly one row.
            MobileDevice.objects.filter(fcm_token=fcm_token).exclude(pk=device.pk).update(
                fcm_token="", fcm_token_updated_at=timezone.now()
            )

        action = "registered" if created else "updated"
        logger.info("Mobile device %s for user %s: %s", action, user.pk, device_uuid)
        return device, created

    @staticmethod
    def unregister(user, device_uuid: str | uuid_module.UUID) -> bool:
        """Clear the FCM token for the user's device, keeping the row for stats; True if one was found.

        Called at sign-out, so a signed-out phone stops receiving the previous user's pushes. Scoped to the
        calling user, so one account can't clear another's token.
        """
        device_uuid = DeviceService._coerce_uuid(device_uuid)
        device = MobileDevice.objects.filter(device_uuid=device_uuid, user=user).first()
        if device is None:
            return False
        device.fcm_token = ""
        device.fcm_token_updated_at = timezone.now()
        device.save(update_fields=["fcm_token", "fcm_token_updated_at"])
        logger.info("Mobile device unregistered (token cleared) for user %s: %s", user.pk, device_uuid)
        return True
