"""Printing from a computer to the phone's Bluetooth label printer.

The user is signed in on a desktop, the app is open on their phone, they press print on the website,
and the labels come out of the phone's printer. If the phone can't be reached, the *computer* says
so and offers to try again, print a PDF instead, or cancel.

Everything here is shaped by one fact: **there is no reliable way to make a phone print on demand
from a server.** Android forbids starting an Activity from the background (API 29+), and a
high-priority data message wakes a headless isolate that has none of the shell's BLE state; iOS
silent pushes are rate-limited, best-effort, and dropped entirely once the app is force-quit, and
CoreBluetooth in the background does not survive a terminated app either. So this does not fire a
push into the void and wait out a timeout. It *measures* whether the phone is awake
(``MobileDevice.print_ready`` + a heartbeat, see :func:`heartbeat`) and tells the user the truth
either way -- including before the push, so a job with no phone to go to is ``unreachable`` on the
page that opens rather than twenty seconds later. What it never does is quietly do something else
instead: the preference decides the page, the heartbeat decides what the page says.

The division of labour, because it is not obvious from any one function:

* the **app** owns the failure vocabulary — it posts the text it would have shown in its own
  snackbar and the website shows that verbatim;
* the **server** owns the presence rule and the job record;
* the **waiting page** owns nothing but polling, so the same job can be watched from two tabs.
"""

import logging

from django.db import transaction
from django.utils import timezone

from auctions.models import MobileDevice, RemotePrintJob
from auctions.notifications import SEND_OK, send_fcm_data_message

logger = logging.getLogger(__name__)

# One push carries the lot pks as a comma string (FCM data values are strings), so a very large batch
# would make an oversized message. FCM's own limit is 4 KB of data; this keeps a comfortable margin
# and matches the deep-link path's cap, which is the same batch coming out of the same printer.
MAX_LOTS_PER_JOB = 300


def heartbeat(user, device_uuid, *, print_ready=False, printer_name="", print_method=""):
    """Record one "I'm awake" beat from the app. Returns the device, or None if it isn't registered.

    Scoped to the calling user: a heartbeat can only ever touch a device row that already belongs to
    them, so one account cannot mark another's phone reachable.

    ``ever_print_ready`` only ever goes True. It is what decides whether /printing/ offers the
    checkbox at all, and that question is "does this account have a phone that could do this",
    which does not become False again because the printer happens to be switched off this morning.

    ``print_method`` is accepted and deliberately not stored. The app sends what the phone is set to,
    but ``print_ready`` is the app's own "a printer is paired and its profile resolves" and must not
    be re-derived from a preference — a user can have Bluetooth selected on an account whose phone
    has nothing paired, and believing the preference there would promise a print that fails. The
    canonical copy of the preference is ``UserLabelPrefs``, which the app already syncs separately.
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

    This is what decides *which page* a print goes to, and it is deliberately only half the
    question -- ``MobileDevice.reachable_printers_for`` is the other half, "will it work right now",
    and that one is asked by :func:`create_job` when it looks for a phone to push to and answered on
    the page rather than in this branch.

    Somebody who has asked for their labels to come out of the printer next to their phone has asked
    for that whether or not the app happens to be open this second, and silently handing them a PDF
    instead is the site doing something else without saying so — they find out when no labels
    appear. They get the same page either way now, and it tells them the truth: open the app, then
    Try again, with the PDF still one button away for whoever wants it.
    """
    from auctions.models import UserLabelPrefs

    if not user or not user.is_authenticated:
        return False
    return UserLabelPrefs.objects.filter(user=user, print_from_computer=True).exists()


@transaction.atomic
def create_job(user, lot_pks, device=None):
    """Create a job for *lot_pks* (already in print order) and return it, unpushed.

    Kept separate from :func:`dispatch` so a retry can reuse the lot list without re-deriving it from
    a queryset that may have changed underneath (a lot sold in the meantime would silently shorten
    the batch, and the person is standing at the printer expecting the same labels).
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
    """Push *job* to its phone. Sets ``sent`` or, on a failure already known, ``unreachable``.

    A missing device, a missing token or an FCM error is ``unreachable`` **immediately** rather than
    something the page waits twenty seconds to discover: the answer is already known, and making the
    user watch a spinner for a failure we could name at once is the thing this whole design exists to
    avoid.
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

    The 20-second rule is applied *here* rather than in the page's JavaScript so that two tabs
    watching the same job agree, and so "unreachable" is a fact recorded on the row rather than a
    thing one browser decided. A job that later reports anyway is allowed to move back out of it —
    the phone demonstrably was reachable, and the truth is worth more than the earlier guess.
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
