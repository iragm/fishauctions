"""The advertising admin: campaign groups, the campaigns in one, and what they cost to show.

Split out of ``auctions/admin.py`` for the same reason ``moderation_admin`` lives on its own: the
ads are a self-contained feature and nothing else in the admin touches them. Imported by
``admin.py`` for the side effect of registering these two pages.

Both changelists print counts over ``AdCampaignResponse`` -- a table with a row per ad ever shown --
for every row they list, so both annotate them: ``AdCampaign.annotate_response_counts`` and
``AdCampaignGroup.annotate_totals``, declared next to the properties that read them.

The campaigns **inline** annotates them too, and not for the reason it looks like: its ``fields``
names five editable columns and ``get_fields`` returns exactly that when it is set, so the
``readonly_fields`` below are never rendered in the inline. The counts get read anyway, because
each row's heading is ``AdCampaign.__str__`` and that prints the click rate -- so two counts a row,
through a string.
"""

from django.contrib import admin

from .admin_performance import FlatInline
from .models import AdCampaign, AdCampaignGroup, AdCampaignResponse


class AdCampaignResponseInline(admin.TabularInline):
    fields = ["user", "clicked", "timestamp"]
    readonly_fields = ["user", "clicked", "timestamp"]
    verbose_name = "Response"
    verbose_name_plural = "Responses"
    model = AdCampaignResponse
    extra = 0


class AdCampaignInline(FlatInline, admin.TabularInline):
    #: `AdCampaign.__str__` names the group -- the very object whose page this is.
    inline_select_related = ("campaign_group",)
    #: Small enough to stay a dropdown, but a dropdown still re-reads its table for every row.
    inline_shared_choices = ("category",)

    def get_queryset(self, request):
        """Each row's heading is `AdCampaign.__str__`, which prints the click rate."""
        return AdCampaign.annotate_response_counts(super().get_queryset(request))

    fields = [
        "title",
        "begin_date",
        "end_date",
        "auction",
        "category",
    ]
    readonly_fields = (
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    )
    verbose_name = "Campaign in this group"
    verbose_name_plural = "Campaigns in this group"
    model = AdCampaign
    extra = 0


class AdCampaignAdmin(admin.ModelAdmin):
    list_select_related = ("campaign_group",)  # named in list_display below

    def get_queryset(self, request):
        """The changelist prints two counts a row; this pays for them once."""
        return AdCampaign.annotate_response_counts(super().get_queryset(request))

    list_display = [
        "title",
        "campaign_group",
        "begin_date",
        "end_date",
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    ]
    # exclude = []
    readonly_fields = (
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    )

    inlines = [
        # AdCampaignResponseInline, # this is far too noisy
    ]
    search_fields = (
        "title",
        "external_url",
    )


class AdCampaignGroupAdmin(admin.ModelAdmin):
    ordering = ("-pk",)  # see AuctionAdmin: AdCampaignAdmin.campaign_group autocompletes to here
    list_select_related = ("contact_user",)  # named in list_display below

    def get_queryset(self, request):
        """Three counts a row, one of them over every ad ever shown; this pays for them once."""
        return AdCampaignGroup.annotate_totals(super().get_queryset(request))

    list_display = [
        "title",
        "contact_user",
        "number_of_campaigns",
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    ]
    # exclude = []
    readonly_fields = (
        "number_of_impressions",
        "number_of_clicks",
        "click_rate",
    )
    inlines = [
        AdCampaignInline,
    ]
    search_fields = ("title",)


admin.site.register(AdCampaign, AdCampaignAdmin)
admin.site.register(AdCampaignGroup, AdCampaignGroupAdmin)
