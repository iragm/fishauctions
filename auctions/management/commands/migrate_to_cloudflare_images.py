"""Move locally stored images to Cloudflare Images.

Only the original upload of each image is sent -- never the locally generated
thumbnail files, which Cloudflare replaces with on-the-fly variants (uploading them
would just cost storage).  Runs automatically every minute via celery beat (a no-op
unless the CLOUDFLARE_IMAGES_* settings in .env are configured); celery's 5 minute
task time limit chunks a large initial migration, and a Redis lock lets the next run
resume where the last one left off.  Run --setup once before anything else to create
the image variants on Cloudflare.
"""

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from easy_thumbnails.files import get_thumbnailer

from auctions import cloudflare_images
from auctions.models import AdCampaign, Club, LotImage

# Lot images first (by far the most numerous and most viewed), then club icons and ads
MODELS_TO_MIGRATE = (LotImage, Club, AdCampaign)

LOCK_KEY = "migrate_to_cloudflare_images_lock"
LOCK_TIMEOUT_SECONDS = 300  # refreshed per image; a crashed run releases the lock within 5 minutes


class Command(BaseCommand):
    help = (
        "Upload all not-yet-migrated original images (lot images, then club icons, then ad campaigns) "
        "to Cloudflare Images and record their ids; does nothing unless CLOUDFLARE_IMAGES_* is set in .env. "
        "Local files are kept as a backup and for fallback if Cloudflare Images is ever disabled."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--count",
            type=int,
            default=None,
            help="Migrate at most this many images in one run (default: all of them)",
        )
        parser.add_argument(
            "--setup",
            action="store_true",
            help="Create or update the named image variants on Cloudflare, then exit; run this once before migrating",
        )
        parser.add_argument(
            "--delete-thumbnails",
            action="store_true",
            help="After each successful upload, delete the locally generated thumbnail files "
            "(originals are kept; thumbnails regenerate automatically if ever needed again)",
        )

    def handle(self, *args, **options):
        if not cloudflare_images.enabled():
            self.stdout.write("Cloudflare Images is not configured (see CLOUDFLARE_IMAGES_* in .env.example)")
            return
        if options["setup"]:
            try:
                cloudflare_images.sync_variants()
            except cloudflare_images.CloudflareImagesError as e:
                raise CommandError(str(e)) from e
            self.stdout.write(self.style.SUCCESS(f"Synced variants: {', '.join(cloudflare_images.VARIANTS)}"))
            return
        if not cache.add(LOCK_KEY, 1, LOCK_TIMEOUT_SECONDS):
            self.stdout.write("Another migration run is already in progress")
            return
        try:
            migrated = self.migrate_images(options)
        finally:
            cache.delete(LOCK_KEY)
        if migrated:
            self.stdout.write(self.style.SUCCESS(f"Migrated {migrated} image{'s' if migrated != 1 else ''}"))
        else:
            self.stdout.write("No unmigrated images found")

    def migrate_images(self, options):
        migrated = 0
        for instance in self.candidates():
            cache.touch(LOCK_KEY, LOCK_TIMEOUT_SECONDS)
            field_file = getattr(instance, instance.IMAGE_FIELD_NAME)
            label = f"{type(instance).__name__} {instance.pk}"
            try:
                image_id = cloudflare_images.upload(
                    field_file, metadata={"model": type(instance).__name__, "pk": instance.pk}
                )
            except OSError as e:
                # local file is missing or unreadable; leave it and move on to the next one
                self.stderr.write(f"Skipping {label}: cannot read {field_file.name}: {e}")
                continue
            except cloudflare_images.CloudflareImagesError as e:
                if e.status_code and 400 <= e.status_code < 500 and e.status_code not in (401, 403, 429):
                    # Cloudflare rejected this particular file (unsupported format, too
                    # large...): mark it so it isn't retried forever; it keeps being
                    # served from the local file, and replacing the image retries.
                    instance.cloudflare_image_id = cloudflare_images.UPLOAD_FAILED
                    instance.save(update_fields=["cloudflare_image_id"])
                    self.stderr.write(f"Cloudflare rejected {label} ({field_file.name}): {e}")
                    continue
                # an API problem (bad token, rate limit, outage...) will affect every upload: stop
                msg = f"Uploading {label} ({field_file.name}) failed: {e}"
                raise CommandError(msg) from e
            instance.cloudflare_image_id = image_id
            instance.save(update_fields=["cloudflare_image_id"])
            if options["delete_thumbnails"]:
                get_thumbnailer(field_file).delete_thumbnails()
            self.stdout.write(self.style.SUCCESS(f"Migrated {label} ({field_file.name}) to {image_id}"))
            migrated += 1
            if options["count"] and migrated >= options["count"]:
                break
        return migrated

    def candidates(self):
        """Instances with a local image file and no Cloudflare id yet, in migration order"""
        for model in MODELS_TO_MIGRATE:
            field = model.IMAGE_FIELD_NAME
            qs = (
                model.objects.filter(cloudflare_image_id="")
                .exclude(**{field: ""})
                .exclude(**{f"{field}__isnull": True})
                .order_by("pk")
            )
            if model is LotImage:
                # don't pay to host images of deleted lots; these become candidates again if restored
                qs = qs.filter(lot_number__is_deleted=False)
            yield from qs.iterator()
