"""Printing from a computer to the phone's Bluetooth label printer.

The user presses print on the website and the labels come out of the phone's printer; if the phone
can't be reached, the computer says so and offers to try again, print a PDF, or cancel.

Everything here is shaped by one fact: **there is no reliable way to make a phone print on demand
from a server.** Android forbids starting an Activity from the background and a data message wakes a
headless isolate with none of the shell's BLE state; iOS silent pushes are rate-limited,
best-effort, and dropped once the app is force-quit. So this doesn't fire a push into the void and
wait out a timeout: it measures whether the phone is awake (``MobileDevice.print_ready`` plus a
heartbeat, see :func:`heartbeat`) and says so, including before the push. What it never does is
quietly do something else instead -- the preference decides the page, the heartbeat decides what the
page says.

The division of labour: the **app** owns the failure vocabulary (the website shows its text
verbatim), the **server** owns the presence rule and the job record, and the **waiting page** owns
nothing but polling, so one job can be watched from two tabs.
"""

import logging

from django.db import transaction
from django.utils import timezone

from auctions.models import MobileDevice, RemotePrintJob
from auctions.notifications import SEND_OK, send_fcm_data_message

logger = logging.getLogger(__name__)

# One push carries the lot pks as a comma string, and FCM's limit is 4 KB of data. This keeps a
# margin and matches the deep-link path's cap, which is the same batch out of the same printer.
MAX_LOTS_PER_JOB = 300


def heartbeat(user, device_uuid, *, print_ready=False, printer_name="", print_method=""):
    """Record one "I'm awake" beat from the app; returns the device, or None if it isn't registered.

    Scoped to the calling user, so one account can't mark another's phone reachable.

    ``ever_print_ready`` only ever goes True: it decides whether /printing/ offers the checkbox at all,
    and that question doesn't become False because the printer is switched off this morning.

    ``print_method`` is accepted and deliberately not stored: ``print_ready`` is the app's own "a
    printer is paired and its profile resolves", and believing a preference instead would promise a
    print that fails. ``UserLabelPrefs`` is the canonical copy.
    """
    device = MobileDevice.objects.filter(device_uuid=device_uuid, user=user).first()
    if device is None:
        return None
    device.last_heartbeat = timezone.now()
    device.print_ready = bool(print_ready)
    device.printer_name = printer_name or ""
    fields = ["last_heartbeat", "print_ready", "printer_name", "last_seen"]
    if print_ready and not device.ever_print_ready:
        device.ever_print_ready = True
        fields.append("ever_print_ready")
    device.save(update_fields=fields)
    return device


def wants_print_from_computer(user):
    """Is this user's ``print_from_computer`` preference on? Says nothing about the phone.

    This decides which page a print goes to; ``MobileDevice.reachable_printers_for`` is the other half,
    asked by :func:`create_job` and answered on the page.

    Somebody who asked for labels from the printer next to their phone asked for that whether or not the
    app is open this second, and silently handing them a PDF is the site doing something else without
    saying so. They get the same page either way, with the PDF one button away.
    """
    from auctions.models import UserLabelPrefs

    if not user or not user.is_authenticated:
        return False
    return UserLabelPrefs.objects.filter(user=user, print_from_computer=True).exists()


@transaction.atomic
def create_job(user, lot_pks, device=None):
    """Create a job for *lot_pks* (already in print order) and return it, unpushed.

    Separate from :func:`dispatch` so a retry reuses the lot list rather than re-deriving it from a
    queryset that may have changed: a lot sold since would silently shorten the batch.
    """
    lot_pks = list(lot_pks)[:MAX_LOTS_PER_JOB]
    if device is None:
        device = MobileDevice.reachable_printers_for(user).first()
    return RemotePrintJob.objects.create(
        user=user,
        device=device,
        lots=lot_pks,
        total_count=len(lot_pks),
        status=RemotePrintJob.STATUS_QUEUED,
    )


def dispatch(job):
    """Push *job* to its phone; sets ``sent``, or ``unreachable`` on a failure already known.

    A missing device, token or FCM error is ``unreachable`` immediately rather than something the page
    waits twenty seconds to discover.
    """
    token = (job.device.fcm_token or "") if job.device else ""
    if not token:
        job.status = RemotePrintJob.STATUS_UNREACHABLE
        job.save(update_fields=["status", "updated_at"])
        return False
    result = send_fcm_data_message(
        token,
        {
            "type": "print_labels",
            "job": str(job.uuid),
            "lots": ",".join(str(pk) for pk in job.lots),
        },
    )
    if result != SEND_OK:
        logger.warning("Remote print job %s could not be pushed to device %s", job.uuid, job.device_id)
        job.status = RemotePrintJob.STATUS_UNREACHABLE
        job.save(update_fields=["status", "updated_at"])
        return False
    job.status = RemotePrintJob.STATUS_SENT
    job.save(update_fields=["status", "updated_at"])
    return True


def start(user, lot_pks):
    """:func:`create_job` + :func:`dispatch`. What the label view calls."""
    job = create_job(user, lot_pks)
    dispatch(job)
    return job


def job_state(job):
    """The polled payload, applying the silence rule as it reads.

    The 20-second rule is applied here rather than in the page's JavaScript, so two tabs agree and
    "unreachable" is a fact on the row. A job that reports anyway moves back out of it.
    """
    if job.has_gone_quiet:
        job.status = RemotePrintJob.STATUS_UNREACHABLE
        job.save(update_fields=["status", "updated_at"])
    return {
        "status": job.status,
        "printed": job.printed_count,
        "total": job.total_count,
        "message": job.message or None,
    }
