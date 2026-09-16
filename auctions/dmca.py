"""The DMCA designated agent, the takedown, and the repeat-infringer policy.

Photographs here are uploaded by sellers, so the site hosts material at its users' direction and
17 U.S.C. 512(c) is what stands between it and their mistakes. That protection is conditional:

1. **A designated agent, registered and published.** 512(c)(2) wants the agent's name, address,
   phone and email filed with the Copyright Office and published on the site. The filing costs $6 at
   https://dmca.copyright.gov/osp/ and lapses after three years. :func:`agent` reads the published
   half out of the environment and ``/dmca/`` renders it; the filing is the operator's job.
2. **Expeditious removal**, which has to take the file, the CDN copy and the edge cache with it --
   :func:`auctions.signals.on_uploaded_image_deleted` and :mod:`auctions.cloudflare_cache`.
3. **A repeat-infringer policy, adopted, published and reasonably implemented** (512(i)(1)(A)):
   :func:`record_strike`, :data:`STRIKES_BEFORE_TERMINATION` and
   :class:`auctions.moderation_models.CopyrightStrike`.

The third strike doesn't terminate an account by itself: 512(f) exists because false notices are
sent, and an automatic ban wired to a number a stranger controls loses somebody their account over a
form. It messages the operator instead, and :func:`terminate` is one click. Cox lost the safe
harbour for having a policy it didn't follow, while *Ventura Content v. Motherless* kept it with no
written procedure at all, because it acted.

Every published value is per-deployment, from ``.env``. A fork with no registered agent publishes
nothing: ``/dmca/`` 404s and the footer link doesn't render.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

#: What ``MAILING_ADDRESS`` says when unset (settings.py); falling back to it would publish "No
#: address configured" as the agent's address.
_UNSET_MAILING_ADDRESS = "No address configured"

#: Strikes before the policy calls for termination. Published in the terms and on /dmca/, so all
#: three have to agree.
STRIKES_BEFORE_TERMINATION = 3


def _setting(name):
    return (getattr(settings, name, "") or "").strip()


def agent():
    """The designated agent block as ``/dmca/`` publishes it, or ``None``.

    ``DMCA_AGENT_EMAIL`` and ``DMCA_AGENT_ADDRESS`` fall back to ``ADMIN_EMAIL`` and
    ``MAILING_ADDRESS``, so the minimum to add after filing is the entity name, the agent's name and a
    phone number. All five are required together: 512(c)(2) names them, and a page with three tells a
    rightsholder they can't reach anybody.
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
    """Whether this deployment has an agent to publish; ``/dmca/`` and the footer link key off this."""
    return agent() is not None


def agent_email():
    """Where a notice goes: the agent's address, or the site admin.

    Separate from :func:`agent` because the notice form still has to reach somebody on a deployment
    that hasn't registered -- that loses the safe harbour, not the mail.
    """
    block = agent()
    if block:
        return block["email"]
    return settings.ADMINS[0][1] if settings.ADMINS else ""


def strike_count(user):
    """How many strikes count against this account now.

    Withdrawn strikes stay on the record and stop counting: a withdrawn notice, or one where the
    material went back up after a counter-notice, is not a strike, and 512(g) would be a trap if it were.
    """
    from auctions.moderation_models import CopyrightStrike

    if not user or not user.pk:
        return 0
    return CopyrightStrike.objects.filter(user=user, withdrawn=False).count()


def record_strike(user, notice=None, reason="", issued_by=None):
    """Add a strike, tell the user, and tell the operator when the policy calls for termination.

    Returns the strike. The caller needn't check the count: the two emails are the whole of what
    happens, and :func:`terminate` is a separate, deliberate act.
    """
    from django.contrib.sites.models import Site
    from django.urls import reverse
    from post_office import mail

    from auctions.moderation_models import CopyrightStrike

    strike = CopyrightStrike.objects.create(user=user, notice=notice, reason=reason, issued_by=issued_by)
    count = strike_count(user)
    domain = Site.objects.get_current().domain
    remaining = STRIKES_BEFORE_TERMINATION - count
    # /dmca/ doesn't exist without a configured agent, so the sentence explaining how to answer a
    # takedown must not link to a 404.
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
    """Remove the material a notice complains about, and record a strike; returns how many images went.

    Deletes every image on the named lot, which is what removes the file, the Cloudflare copy and the
    edge cache (``auctions.signals.on_uploaded_image_deleted``). The lot is left alone: a notice is
    about a photograph, and deleting the listing would remove bids and an auction entry too.
    """
    from auctions.models import LotImage

    removed = 0
    if notice.lot:
        images = list(LotImage.objects.filter(lot_number=notice.lot))
        for image in images:
            image.delete()
        removed = len(images)
        if notice.lot.image:
            # The legacy single image on Lot itself, from before LotImage.
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
    """Close an account for repeat infringement: the third-strike action, done by a person.

    ``is_active = False`` is the switch account deletion uses, so everything the account is part of
    stays where it is.
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
