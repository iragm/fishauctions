"""The Django admin for the moderation queue: reports, copyright notices and strikes.

Split out of ``admin.py`` because that file is at the size ceiling ``auctions/module_map.py``
holds it to, and imported back into it so the registrations still happen at app load.

The interesting one is :class:`CopyrightNoticeAdmin.take_down_material`. Everything else here is a
changelist; that action is the compliance path in one button, and the reason it is a button is that
doing its three steps by hand reliably means doing two of them.
"""

from django.contrib import admin, messages
from django.utils import timezone

from auctions.models import ContentReport, CopyrightNotice, CopyrightStrike


@admin.register(ContentReport)
class ContentReportAdmin(admin.ModelAdmin):
    """The report queue. App Store Review Guideline 1.2 asks for "timely responses to concerns",
    which in practice means somebody has to be able to see the open ones, so ``status`` leads the
    filters and the list opens newest first."""

    list_display = ("createdon", "reason", "material", "status", "reported_by")
    list_filter = ("status", "reason", "createdon")
    list_select_related = ("reported_by", "lot")
    search_fields = ("material", "details", "reporter_email", "reported_by__username")
    raw_id_fields = ("lot", "reported_by", "resolved_by")
    readonly_fields = ("createdon",)
    actions = ["mark_actioned", "mark_dismissed"]

    @admin.action(description="Mark as actioned")
    def mark_actioned(self, request, queryset):
        updated = queryset.update(status="ACTIONED", resolved_by=request.user, resolved_on=timezone.now())
        messages.success(request, f"{updated} report(s) marked actioned.")

    @admin.action(description="Dismiss")
    def mark_dismissed(self, request, queryset):
        updated = queryset.update(status="DISMISSED", resolved_by=request.user, resolved_on=timezone.now())
        messages.success(request, f"{updated} report(s) dismissed.")


@admin.register(CopyrightNotice)
class CopyrightNoticeAdmin(admin.ModelAdmin):
    """DMCA notices, and the one button that makes the safe harbour true.

    ``take_down_material`` is the whole compliance path in one action: it deletes the images on the
    lot the notice names -- which is what actually removes the file, the Cloudflare copy and the
    cached copy at the edge, see ``auctions.signals.on_uploaded_image_deleted`` -- records a strike
    against the seller, and emails them. Doing those three by hand is doing two of them.

    ``is_complete`` is on the list because it is the first question about any notice: an incomplete
    one does not start the removal clock under 512(c)(3)(B). It is still usually worth acting on.
    """

    list_display = ("createdon", "name", "on_behalf_of", "status", "notice_is_complete", "lot")
    list_filter = ("status", "good_faith", "accurate", "createdon")
    list_select_related = ("lot", "submitted_by")
    search_fields = ("name", "email", "work", "material", "on_behalf_of")
    raw_id_fields = ("lot", "submitted_by")
    readonly_fields = ("createdon",)
    actions = ["take_down_material", "mark_invalid", "mark_withdrawn"]

    @admin.display(boolean=True, description="Complete notice")
    def notice_is_complete(self, obj):
        return obj.is_complete

    @admin.action(description="Take the material down and record a strike")
    def take_down_material(self, request, queryset):
        from auctions import dmca

        for notice in queryset:
            if not notice.lot:
                messages.warning(
                    request,
                    f"Notice from {notice.name} isn't linked to a lot, so there is nothing to "
                    f"remove automatically. Set the lot on the notice, or remove the material by hand.",
                )
                continue
            removed = dmca.take_down(notice, admin=request.user)
            messages.success(
                request,
                f"Removed {removed} image(s) from lot {notice.lot.lot_number} and recorded a strike.",
            )

    @admin.action(description="Reject as not a valid notice")
    def mark_invalid(self, request, queryset):
        updated = queryset.update(status="INVALID", actioned_on=timezone.now())
        messages.success(request, f"{updated} notice(s) rejected.")

    @admin.action(description="Mark withdrawn by the sender")
    def mark_withdrawn(self, request, queryset):
        updated = queryset.update(status="WITHDRAWN", actioned_on=timezone.now())
        messages.success(request, f"{updated} notice(s) marked withdrawn.")


@admin.register(CopyrightStrike)
class CopyrightStrikeAdmin(admin.ModelAdmin):
    """The 512(i) record: what the repeat-infringer policy actually did, per account.

    The reason this table exists rather than a counter on ``UserData`` is that the counter cannot
    be audited. What a court asks is whether the policy was reasonably implemented, and the answer
    is a list of decisions with dates and reasons on them.
    """

    list_display = ("createdon", "user", "strikes_against_this_user", "withdrawn", "notice")
    list_filter = ("withdrawn", "createdon")
    list_select_related = ("user", "notice", "issued_by")
    search_fields = ("user__username", "user__email", "reason")
    raw_id_fields = ("user", "notice", "issued_by")
    readonly_fields = ("createdon",)
    actions = ["close_account_for_repeat_infringement", "withdraw_strike"]

    @admin.display(description="Strikes on this account")
    def strikes_against_this_user(self, obj):
        from auctions import dmca

        return dmca.strike_count(obj.user)

    @admin.action(description="Close the account for repeat infringement")
    def close_account_for_repeat_infringement(self, request, queryset):
        from auctions import dmca

        for user in {strike.user for strike in queryset.select_related("user")}:
            count = dmca.strike_count(user)
            if count < dmca.STRIKES_BEFORE_TERMINATION:
                messages.warning(
                    request,
                    f"{user.username} has {count} strike(s); the published policy closes an account "
                    f"at {dmca.STRIKES_BEFORE_TERMINATION}. Closing anyway is a decision to make on "
                    f"purpose -- do it from the user admin, and write down why here.",
                )
                continue
            dmca.terminate(user, admin=request.user, reason=f"{count} copyright strikes.")
            messages.success(request, f"Closed {user.username}'s account.")

    @admin.action(description="Withdraw (stops counting, stays on the record)")
    def withdraw_strike(self, request, queryset):
        updated = queryset.update(withdrawn=True)
        messages.success(request, f"{updated} strike(s) withdrawn.")
