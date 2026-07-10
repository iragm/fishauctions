"""Apple PassKit web service — the endpoints installed Wallet passes talk to.

Every .pkpass we generate embeds webServiceURL (https://<domain>/passkit) and a
per-member authenticationToken (ClubMember.apple_pass_auth_token).  iOS appends
/v1/... to that base URL, so the five routes here implement Apple's Web Service
Reference verbatim:

    POST   /passkit/v1/devices/<dlid>/registrations/<passTypeId>/<serial>  register device
    DELETE /passkit/v1/devices/<dlid>/registrations/<passTypeId>/<serial>  unregister device
    GET    /passkit/v1/devices/<dlid>/registrations/<passTypeId>           serials updated since tag
    GET    /passkit/v1/passes/<passTypeId>/<serial>                        latest .pkpass
    POST   /passkit/v1/log                                                 device error reports

The update flow: a signal notices a wallet-visible change, a Celery task bumps
ClubMember.apple_pass_updated and pokes APNs (auctions/apple_wallet.py), the
device calls the registrations endpoint to learn which serials changed, then
re-fetches each pass from the passes endpoint.

Endpoints that take a serial authenticate with "Authorization: ApplePass
<token>"; the registrations-list endpoint has no auth header in Apple's spec
(the device library identifier, generated on-device, is the capability).  CSRF
is exempted — callers are iPhones, not browsers with our session cookies.
"""

import json
import logging

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.utils.crypto import constant_time_compare
from django.utils.decorators import method_decorator
from django.utils.http import http_date, parse_http_date_safe
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import AppleDeviceRegistration, ClubMember

logger = logging.getLogger(__name__)


def _member_from_serial(pass_type_id, serial_number):
    """Resolve a serial like "member-123" to its ClubMember, or None.

    Soft-deleted members are included on purpose: their pass must keep being
    served (voided) so installed copies gray out instead of erroring forever.
    """
    from .apple_wallet import is_configured

    if not is_configured() or pass_type_id != settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER:
        return None
    prefix, _, pk = serial_number.partition("-")
    if prefix != "member" or not pk.isdigit():
        return None
    return ClubMember.objects.filter(pk=int(pk)).select_related("club", "user").first()


def _is_authorized(request, member):
    """Check the ApplePass auth header against the member's pass token.

    An empty stored token (pass never generated) can never authorize — otherwise
    a blank Authorization header would compare equal to it.
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return (
        scheme == "ApplePass"
        and bool(member.apple_pass_auth_token)
        and constant_time_compare(token.strip(), member.apple_pass_auth_token)
    )


def _update_tag(member) -> int:
    """The member's pass version as a unix timestamp (whole seconds).

    Used both as the Last-Modified value on pass delivery and as the
    lastUpdated / passesUpdatedSince tag on the registrations endpoint, so the
    two update channels can never disagree.
    """
    return int(member.apple_pass_updated.timestamp())


@method_decorator(csrf_exempt, name="dispatch")
class PassKitRegistrationView(View):
    """Register (POST) or unregister (DELETE) one device for one pass."""

    def post(self, request, device_library_id, pass_type_id, serial_number):
        member = _member_from_serial(pass_type_id, serial_number)
        if member is None:
            raise Http404
        if not _is_authorized(request, member):
            return HttpResponse(status=401)
        try:
            push_token = json.loads(request.body or b"{}").get("pushToken", "")
        except ValueError:
            push_token = ""
        if not push_token:
            return HttpResponse(status=400)
        # update_or_create rather than get_or_create: APNs tokens rotate (device
        # restore, OS update) and the registration must track the newest one.
        _registration, created = AppleDeviceRegistration.objects.update_or_create(
            member=member,
            device_library_identifier=device_library_id,
            defaults={"push_token": push_token},
        )
        return HttpResponse(status=201 if created else 200)

    def delete(self, request, device_library_id, pass_type_id, serial_number):
        member = _member_from_serial(pass_type_id, serial_number)
        if member is None:
            raise Http404
        if not _is_authorized(request, member):
            return HttpResponse(status=401)
        AppleDeviceRegistration.objects.filter(member=member, device_library_identifier=device_library_id).delete()
        return HttpResponse(status=200)


class PassKitDeviceRegistrationsView(View):
    """List serial numbers of this device's passes updated since a tag.

    204 = registered but nothing new; 404 = this device holds none of our passes.
    """

    def get(self, request, device_library_id, pass_type_id):
        from .apple_wallet import is_configured

        if not is_configured() or pass_type_id != settings.APPLE_WALLET_PASS_TYPE_IDENTIFIER:
            raise Http404
        members = [
            registration.member
            for registration in AppleDeviceRegistration.objects.filter(
                device_library_identifier=device_library_id
            ).select_related("member")
        ]
        if not members:
            raise Http404
        try:
            since = int(request.GET.get("passesUpdatedSince", ""))
        except ValueError:
            since = None
        updated = [member for member in members if since is None or _update_tag(member) > since]
        if not updated:
            return HttpResponse(status=204)
        return JsonResponse(
            {
                "serialNumbers": [f"member-{member.pk}" for member in updated],
                "lastUpdated": str(max(_update_tag(member) for member in updated)),
            }
        )


class PassKitPassView(View):
    """Serve the freshest signed .pkpass for a serial.

    Unlike the user-facing download views, this must NOT 404 when barcodes are
    turned off or the member was deactivated — the pass is served voided instead
    (see _build_pass_json), which is the only way to kill an installed pass.
    """

    def get(self, request, pass_type_id, serial_number):
        from .apple_wallet import generate_pkpass_for_member

        member = _member_from_serial(pass_type_id, serial_number)
        if member is None:
            raise Http404
        if not _is_authorized(request, member):
            return HttpResponse(status=401)
        modified = _update_tag(member)
        if_modified_since = parse_http_date_safe(request.headers.get("If-Modified-Since", ""))
        if if_modified_since is not None and modified <= if_modified_since:
            return HttpResponse(status=304)
        response = HttpResponse(generate_pkpass_for_member(member), content_type="application/vnd.apple.pkpass")
        response["Last-Modified"] = http_date(modified)
        response["Cache-Control"] = "private, no-store"
        return response


@method_decorator(csrf_exempt, name="dispatch")
class PassKitLogView(View):
    """Devices report pass errors here (bad signature, fetch failures, ...).

    Always 200 — this is fire-and-forget telemetry, but it is the only
    visibility Apple gives into why a pass misbehaves on-device, so log it.
    """

    def post(self, request):
        try:
            logs = json.loads(request.body or b"{}").get("logs", [])
        except ValueError:
            logs = []
        for line in logs[:20]:
            logger.warning("PassKit device log: %s", line)
        return HttpResponse(status=200)
