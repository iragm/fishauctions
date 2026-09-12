"""The DMCA designated agent, the takedown, and the repeat-infringer policy.

Photographs on this site are uploaded by the people selling the fish, so the site is a service
provider hosting material at the direction of its users, and 17 U.S.C. 512(c) is what stands
between it and their mistakes. That protection is conditional, and the conditions are chores rather
than judgement calls:

1. **A designated agent, registered and published.** 512(c)(2) requires the agent's name, address,
   phone number and email to be filed with the Copyright Office *and* made available to the public
   on the site. The filing costs $6 at https://dmca.copyright.gov/osp/ and lapses after three
   years. This module reads the published half out of the environment (:func:`agent`) and
   ``/dmca/`` renders it; the filing is the operator's job and no code can do it for them.
2. **Expeditious removal.** Deleting the row has to delete the file, the CDN copy and the cached
   copy at the edge, or the material is still there. That part lives in
   :func:`auctions.signals.on_uploaded_image_deleted` and :mod:`auctions.cloudflare_cache`.
3. **A repeat-infringer policy, adopted, published, and reasonably implemented.** 512(i)(1)(A).
   :func:`record_strike` and :data:`STRIKES_BEFORE_TERMINATION` are the implementation, and
   :class:`auctions.moderation_models.CopyrightStrike` is the evidence that it ran.

Why the third strike does not terminate an account by itself: 512(f) exists because false notices
are sent, and an automatic ban wired to a number a stranger controls is a way to lose somebody
their account over a form. So the third strike sends the operator a message saying the policy calls
for termination and :func:`terminate` does it in one click. The strike table records both halves,
which is the thing a court actually asks for -- Cox lost the safe harbour for having a written
policy it did not follow, while the one-man site in *Ventura Content v. Motherless* kept it with no
written procedure at all, because it acted.

Every value published here is per-deployment, out of ``.env``. A fork that has not registered an
agent publishes nothing: ``/dmca/`` 404s and the footer link does not render, which is honest, and
much better than a fork publishing this site's agent as though it were their own.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

#: What ``MAILING_ADDRESS`` says when nobody has set it (see settings.py). Falling back to it would
#: publish the words "No address configured" as the agent's address, which is worse than 404ing.
_UNSET_MAILING_ADDRESS = "No address configured"

#: Strikes an account may collect before the policy calls for terminating it. Published in the
#: terms and in the /dmca/ page, and the number in all three places has to stay the same one.
STRIKES_BEFORE_TERMINATION = 3


def _setting(name):
    return (getattr(settings, name, "") or "").strip()


def agent():
    """The designated agent block as ``/dmca/`` publishes it, or ``None`` if none is configured.

    ``DMCA_AGENT_EMAIL`` and ``DMCA_AGENT_ADDRESS`` fall back to ``ADMIN_EMAIL`` and
    ``MAILING_ADDRESS``, which most deployments have already set to the right thing -- so the
    minimum an operator has to add after filing is the legal entity name, the agent's name and a
    phone number.

    All five values are required together. 512(c)(2) names exactly these four as what has to be
    published, and a page carrying three of them tells a rightsholder they cannot reach anybody.
    """
    email = (
        _setting("DMCA_AGENT_EMAIL") or _setting("ADMIN_EMAIL") or (settings.ADMINS[0][1] if settings.ADMINS else "")
    )
    address = _setting("DMCA_AGENT_ADDRESS") or _setting("MAILING_ADDRESS")
    if address == _UNSET_MAILING_ADDRESS:
        address = ""
    block = {
        "service_provider": _setting("DMCA_SERVICE_PROVIDER_NAME"),
        "name": _setting("DMCA_AGENT_NAME"),
        "phone": _setting("DMCA_AGENT_PHONE"),
        "email": email,
        "address": address,
    }
    if not all(block.values()):
        return None
    return block


def is_configured():
    """Whether this deployment has an agent to publish. ``/dmca/`` and the footer link key off this."""
    return agent() is not None


def agent_email():
    """Where a notice goes. The agent's address when there is one, the site admin otherwise.

    Separate from :func:`agent` because the notice form and the report button still have to reach
    somebody on a deployment that has not registered -- not being in the directory is a reason to
    lose the safe harbour, not a reason to drop the mail.
    """
    block = agent()
    if block:
        return block["email"]
    return settings.ADMINS[0][1] if settings.ADMINS else ""


def strike_count(user):
    """How many strikes count against this account right now.

    Withdrawn strikes stay on the record and stop counting -- a notice that was withdrawn, or one
    where the material went back up after a counter-notice, is not a strike, and 512(g) would be a
    trap if it were.
    """
    from auctions.moderation_models import CopyrightStrike

    if not user or not user.pk:
        return 0
    return CopyrightStrike.objects.filter(user=user, withdrawn=False).count()


def record_strike(user, notice=None, reason="", issued_by=None):
    """Add a strike, tell the user, and tell the operator when the policy calls for termination.

    Returns the strike. The caller does not need to look at the count: the two emails this sends
    are the whole of what happens at each step, and :func:`terminate` is a separate, deliberate act.
    """
    from django.contrib.sites.models import Site
    from django.urls import reverse
    from post_office import mail

    from auctions.moderation_models import CopyrightStrike

    strike = CopyrightStrike.objects.create(user=user, notice=notice, reason=reason, issued_by=issued_by)
    count = strike_count(user)
    domain = Site.objects.get_current().domain
    remaining = STRIKES_BEFORE_TERMINATION - count
    # /dmca/ does not exist on a deployment with no agent configured, so the sentence telling
    # somebody how to answer a takedown must not be a link to a 404.
    counter_notice_route = (
        f"How to do that is at https://{domain}{reverse('dmca')}" if is_configured() else f"Write to {agent_email()}."
    )

    if user.email:
        if remaining > 0:
            consequence = (
                f"This is strike {count} of {STRIKES_BEFORE_TERMINATION}. "
                f"{'One more' if remaining == 1 else f'{remaining} more'} and your account will be closed."
            )
        else:
            consequence = (
                f"This is strike {count}. Our policy is to close accounts at "
                f"{STRIKES_BEFORE_TERMINATION} strikes, and yours is being reviewed for closure now."
            )
        mail.send(
            user.email,
            subject=f"Copyright notice about your listing on {domain}",
            message=(
                f"We received a copyright complaint about material you posted on {domain}, and we "
                f"have removed it.\n\n{reason}\n\n{consequence}\n\n"
                f"If you believe the material was removed by mistake -- because you took the photo "
                f"yourself, or you have permission to use it -- you can send us a counter-notice. "
                f"{counter_notice_route}\n\n"
                f"Please only upload photos you took yourself, or that you have permission to use."
            ),
        )

    if count >= STRIKES_BEFORE_TERMINATION:
        # Deliberately a message and not an action. See the module docstring.
        logger.warning("Account %s has reached %s copyright strikes", user.pk, count)
        admin_email = settings.ADMINS[0][1] if settings.ADMINS else ""
        if admin_email:
            mail.send(
                admin_email,
                subject=f"{user.username} has {count} copyright strikes",
                message=(
                    f"{user.username} has now collected {count} copyright strikes, and the "
                    f"published policy is to close an account at {STRIKES_BEFORE_TERMINATION}.\n\n"
                    f"Review the strikes and close the account at "
                    f"https://{domain}/admin/auctions/copyrightstrike/?user__id__exact={user.pk}\n\n"
                    f"A policy that is written down and not followed is worse than no policy: that "
                    f"is what lost Cox its safe harbour. If this account should not be closed, "
                    f"withdraw a strike and write down why."
                ),
            )
    return strike


def take_down(notice, admin=None):
    """Remove the material a notice complains about, and record a strike for it.

    Deletes every image on the lot the notice names. The row deletions are what actually remove the
    file, the Cloudflare Images copy and the cached copy at the edge -- see
    ``auctions.signals.on_uploaded_image_deleted``. The lot itself is left alone: a notice is about
    a photograph, and deleting somebody's listing along with it removes bids and an auction entry
    that were nothing to do with the complaint.

    Returns the number of images removed.
    """
    from auctions.models import LotImage

    removed = 0
    if notice.lot:
        images = list(LotImage.objects.filter(lot_number=notice.lot))
        for image in images:
            image.delete()
        removed = len(images)
        if notice.lot.image:
            # The legacy single image on Lot itself, from before LotImage existed.
            notice.lot.image.delete(save=True)

    notice.status = "REMOVED"
    notice.actioned_on = timezone.now()
    notice.save()

    if notice.lot and notice.lot.user:
        record_strike(
            notice.lot.user,
            notice=notice,
            reason=(
                f"Copyright complaint about images on lot {notice.lot.lot_number} "
                f"({notice.lot.lot_name}). Reported work: {notice.work[:500]}"
            ),
            issued_by=admin,
        )
    return removed


def terminate(user, admin=None, reason=""):
    """Close an account for repeat infringement. The third-strike action, done by a person.

    ``is_active = False`` is the same switch account deletion uses: the account cannot be signed
    into again, and everything it is part of -- invoices, past auctions, other people's records --
    stays exactly where it is.
    """
    from django.contrib.sites.models import Site
    from post_office import mail

    user.is_active = False
    user.save()
    logger.warning("Closed account %s for repeat copyright infringement (by %s)", user.pk, admin)
    if user.email:
        domain = Site.objects.get_current().domain
        mail.send(
            user.email,
            subject=f"Your {domain} account has been closed",
            message=(
                f"Your account has been closed because we received repeated copyright complaints "
                f"about material you posted.\n\n{reason}\n\n"
                f"If you believe this is a mistake, write to {agent_email()}."
            ),
        )
