"""Pages that belong to the site rather than to any auction or club.

The FAQ, support, the promo site, the privacy policy, the blog, unsubscribe, and the landing
redirect that decides where a signed-in user with no context should be sent.
"""

import logging
import re
from datetime import timedelta
from random import randint

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.db.models import (
    Case,
    Exists,
    F,
    FloatField,
    IntegerField,
    OuterRef,
    Q,
    Value,
    When,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.generic import DetailView, ListView, RedirectView, TemplateView, View
from django.views.generic.edit import (
    FormView,
)

from auctions.filters import (
    AuctionFilter,
    LotFilter,
)
from auctions.forms import (
    ContactForm,
)
from auctions.models import (
    FAQ,
    PRIVACY_POLICY_SLUG,
    Auction,
    AuctionTOS,
    BlogPost,
    Invoice,
    Lot,
    SearchHistory,
    UserData,
)
from auctions.tables import (
    AuctionHTMxTable,
)

from .auction_pages import AuctionInfo
from .base import MILES_TO_KM, AuctionViewMixin, HTMxTableView, LocationMixin
from .browse import LotListView

logger = logging.getLogger(__name__)


class FAQ(ListView):
    """Show all questions"""

    model = FAQ
    template_name = "faq.html"
    ordering = ["category_text"]

    def get_queryset(self):
        """Everything but the agent-only answers.

        ``agent_only`` is not privacy -- anybody can reach one by asking the assistant, and
        ``search_help`` serves them to every caller. It is about what deserves a heading on a page
        somebody reads top to bottom: an edge case that is worth writing down and worth keeping out
        of the twenty questions everybody else came here for.
        """
        return super().get_queryset().filter(agent_only=False)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_site = Site.objects.get_current()
        context["domain"] = current_site.domain
        context["hide_google_login"] = True
        return context


class SupportView(FormView):
    """Every way to get help, on one page, ending in a way to reach a human with no account.

    The page leads with connecting an AI agent (``/ai/``), because that answers a question about
    somebody's own auction in seconds where an email answers it in days. Then the FAQ, then the two
    tutorial videos -- collapsed, because they are half an hour of video and the people who want
    them know they want them -- and last the message form.

    The form is the part with a rule attached. The App Store's Support URL is opened by App Review
    in a plain browser with no session, and the only page that could serve as one was /faq/, which
    ended with the site owner's address for signed-in users and the words "(Sign in to see email)"
    for everybody else. That is a Guideline 1.5 metadata rejection waiting to happen, and a metadata
    rejection costs a review round trip.

    Hiding the address from anonymous visitors is a real measure against scrapers and stays exactly
    as it was: this page never renders it. The message is emailed to ``settings.ADMINS[0][1]`` with
    the sender's address as ``Reply-To``, so answering is one click and nothing is published.

    Deliberately open to everybody, signed in or not -- a support page that needs an account is not
    a support page. reCAPTCHA (the same invisible v2 as signup) is what stands in for the login.
    Everything above the form is on the same page rather than behind a link, so the no-session
    reader gets the whole of it; the one link that needs an account is the agent one, and an agent
    connected to nothing is no use to somebody who has not signed up yet anyway.

    Lives at /support/; /contact/ is a permanent redirect, since that is the address the App Store
    metadata and older links carry.
    """

    template_name = "support.html"
    form_class = ContactForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # The same two videos the promo page shows, and the same chapter lists -- one auction runs
        # online and one in person, and which one somebody needs is the first thing they know.
        context["online_tutorial"] = settings.ONLINE_TUTORIAL_YOUTUBE_ID
        context["online_tutorial_chapters"] = settings.ONLINE_TUTORIAL_CHAPTERS
        context["in_person_tutorial"] = settings.IN_PERSON_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial_chapters"] = settings.IN_PERSON_TUTORIAL_CHAPTERS
        return context

    #: Messages one address can send in an hour. reCAPTCHA is the front door and this is the floor
    #: under it: a site with no keys configured has no captcha at all, and a solved captcha is not
    #: a promise that the next thousand messages are worth reading. Deliberately generous -- a
    #: person with a real problem writes two or three, not six.
    MESSAGES_PER_HOUR = 5

    def _over_the_limit(self, request) -> bool:
        from auctions.mobile.services.ar import _client_ip

        key = f"contact-form:{_client_ip(request) or 'unknown'}"
        count = cache.get_or_set(key, 0, timeout=3600)
        if count >= self.MESSAGES_PER_HOUR:
            return True
        try:
            cache.incr(key)
        except ValueError:  # the window expired between the read and the increment
            cache.set(key, 1, timeout=3600)
        return False

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_success_url(self):
        return reverse("support")

    def form_valid(self, form):
        from post_office import mail

        if self._over_the_limit(self.request):
            # Say so rather than pretending it was sent: somebody who has genuinely written five
            # messages in an hour needs to know the sixth is not on its way.
            messages.error(
                self.request,
                "That's a lot of messages in a short time - please give us a little while to reply "
                "to the ones you've already sent.",
            )
            return super().form_valid(form)

        # Off the account when they are signed in, off the form when they are not -- the form
        # doesn't even render the two fields to somebody it already knows. See ContactForm.
        name = form.sender_name
        email = form.sender_email
        signed_in = self.request.user.username if self.request.user.is_authenticated else "not signed in"
        mail.send(
            settings.ADMINS[0][1],
            subject=f"Contact form: {name}",
            message=(
                f"{name} <{email}> wrote from {Site.objects.get_current().domain}"
                f" ({signed_in}):\n\n{form.cleaned_data['message']}"
            ),
            # Reply-To rather than From: the From address is the site's own routed sender (and on
            # SES it is rewritten anyway), so putting a visitor's address there would fail SPF and
            # land the one email that matters in spam.
            headers={"Reply-To": email},
        )
        messages.success(
            self.request,
            f"Thanks - your message is on its way. We'll reply to {email}.",
        )
        return super().form_valid(form)


class PromoSite(TemplateView):
    template_name = "promo.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.ENABLE_PROMO_PAGE:
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        context["online_tutorial"] = settings.ONLINE_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial"] = settings.IN_PERSON_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial_chapters"] = settings.IN_PERSON_TUTORIAL_CHAPTERS
        context["online_tutorial_chapters"] = settings.ONLINE_TUTORIAL_CHAPTERS
        return context


class ToDefaultLandingPage(View):
    """
    Allow the user to pick up where they left off
    """

    def tos_check(self, request, auction, routeByLastAuction):
        if not auction:
            if request.user.is_authenticated:
                return AllLots.as_view()(request)
            else:
                if settings.ENABLE_PROMO_PAGE:
                    return PromoSite.as_view()(request)
                else:
                    return AllAuctions.as_view()(request)
        # Only check TOS if authenticated
        if request.user.is_authenticated and AuctionTOS.objects.filter(user=request.user, auction=auction).exists():
            return AllLots.as_view(
                rewrite_url=f"/?{auction.slug}",
                auction=auction,
                routeByLastAuction=routeByLastAuction,
            )(request)
        # Anonymous or not joined – send to auction info page
        return AuctionInfo.as_view(rewrite_url=f"/?{auction.slug}", auction=auction)(request)

    def get(self, request, *args, **kwargs):
        data = request.GET.copy()
        routeByLastAuction = False
        if request.user.is_authenticated:
            try:
                userData = request.user.userdata
                userData.last_activity = timezone.now()
                userData.save()
            except AttributeError:
                # probably not signed in
                pass
        try:
            # if the slug was set in the URL
            auction = Auction.objects.exclude(is_deleted=True).filter(slug=list(data.keys())[0])[0]
            # return tos_check(request, auction, routeByLastAuction)
        except Exception:
            # if not, check and see if the user has been participating in an auction
            try:
                auction = UserData.objects.get(user=request.user).last_auction_used
                # Admins of an in-person auction land on the users list, not the lot list — but only
                # while the auction is still current. Once it's pretty_much_over (wound down 24h+),
                # that redirect is stale, so fall through to the invoice/browse path instead.
                if (
                    auction
                    and not auction.is_online
                    and not auction.pretty_much_over
                    and auction.permission_check(request.user)
                ):
                    return redirect(auction.user_admin_link)
                invoice = (
                    Invoice.objects.filter(auctiontos_user__user=request.user, auctiontos_user__auction=auction)
                    .exclude(status="DRAFT")
                    .first()
                )
                if invoice:
                    messages.info(
                        request,
                        format_html(
                            '{} has ended.  <a href="{}">View your invoice</a> or <a href="{}">leave feedback</a> on lots you bought or sold',
                            auction,
                            reverse("invoice_by_pk", kwargs={"pk": invoice.pk}),
                            reverse("feedback"),
                        ),
                        extra_tags="safe",
                    )
                    return redirect(reverse("allLots"))
                else:
                    # in progress online auctions get routed
                    if AuctionTOS.objects.filter(user=request.user, auction=auction, auction__is_online=True).exists():
                        # only show the banner if the TOS is signed
                        # messages.add_message(request, messages.INFO, f'{auction} is the last auction you joined.  <a href="/lots/">View all lots instead</a>')
                        routeByLastAuction = True
            except (TypeError, AttributeError, Auction.DoesNotExist):
                # probably no userdata or userdata.auction is None
                auction = None
        return self.tos_check(request, auction, routeByLastAuction)


class MyAccount(LoginRequiredMixin, RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        return reverse("userpage", kwargs={"slug": self.request.user.username})


class AccountSetupRedirect(LoginRequiredMixin, RedirectView):
    """/account/setup/ -- the one "Account" row in the navbar menu.

    The Account setup menu has no page of its own: it is a sidebar beside whichever of its pages you
    are on. So this lands on the page you were last on, and on Contact info the first time -- the
    one people arrive for, and the one an auction needs filled in. `account_nav.landing_url` checks
    the remembered name against the menu before reversing it.
    """

    def get_redirect_url(self, *args, **kwargs):
        from auctions import account_nav

        return account_nav.landing_url(self.request)


class MyLastAuctionLots(LoginRequiredMixin, RedirectView):
    """GET /lots/my-last-auction/ — the app's "Lots in my last auction" home-screen shortcut.

    Redirects to the lot list filtered to the user's last-used auction when there is one (and it
    hasn't been deleted), otherwise to the plain lot list. Kept server-side so the app can deep-link
    a stable URL without knowing the user's current auction.
    """

    def get_redirect_url(self, *args, **kwargs):
        lots_url = reverse("allLots")
        auction = self.request.user.userdata.last_auction_used
        if auction and not auction.is_deleted:
            return f"{lots_url}?auction={auction.slug}"
        return lots_url


class AllAuctions(LocationMixin, HTMxTableView):
    model = Auction
    no_location_message = "Set your location to see how far away auctions are"
    table_class = AuctionHTMxTable
    filterset_class = AuctionFilter
    template_name = "all_auctions.html"
    htmx_table_header_template = "auctions/partials/all_auctions_table_header.html"
    # paginate_by = 100

    def get_queryset(self):
        last_auction_pk = -1
        if self.request.user.is_authenticated and self.request.user.userdata.last_auction_used:
            last_auction_pk = self.request.user.userdata.last_auction_used.pk
        qs = (
            Auction.objects.all()
            .annotate(
                is_last_used=Case(
                    When(pk=last_auction_pk, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            )
            .order_by("-is_last_used", "-date_start")
        )
        next_90_days = timezone.now() + timedelta(days=90)
        two_years_ago = timezone.now() - timedelta(days=365 * 2)
        standard_filter = Q(
            promote_this_auction=True,
            date_start__lte=next_90_days,
            date_posted__gte=two_years_ago,
        )
        latitude, longitude = self.get_coordinates()
        self._user_has_location = bool(latitude and longitude)
        if latitude and longitude:
            qs = qs.annotate(distance=Auction.get_closest_location_distance_subquery(latitude, longitude))
        else:
            qs = qs.annotate(distance=Value(0, output_field=FloatField()))
        if not self.request.user.is_authenticated:
            qs = qs.exclude(is_deleted=True)
            return qs.filter(standard_filter).annotate(joined=Value(0, output_field=FloatField())).distinct()
        if self.request.user.is_superuser:
            # joined is disabled for admins because we need to return before filtering non-promoted auctions
            return qs.annotate(joined=Value(0, output_field=FloatField())).order_by("-date_posted").distinct()
        qs = qs.exclude(is_deleted=True)
        joined_subquery = Exists(
            AuctionTOS.objects.filter(
                Q(user=self.request.user) | Q(email=self.request.user.email),
                auction=OuterRef("pk"),
            )
        )
        qs = (
            qs.filter(
                Q(auctiontos__user=self.request.user)
                | Q(auctiontos__email=self.request.user.email)
                | Q(created_by=self.request.user)
                | standard_filter
            )
            .annotate(joined=joined_subquery)
            .distinct()
        )
        # Apply nearby filter if user has a location set, the preference is enabled, and nearby=false is not in GET params
        self.nearby_filter_active = False
        userdata = self.request.user.userdata
        self._base_qs = qs  # save pre-filter qs for auto-remove fallback
        if latitude and longitude and userdata.show_nearby_auctions and self.request.GET.get("nearby") != "false":
            online_distance = userdata.email_me_about_new_auctions_distance or 100
            in_person_distance = userdata.email_me_about_new_in_person_auctions_distance or 100
            qs = qs.annotate(
                preferred_distance=Case(
                    When(is_online=True, then=Value(online_distance)),
                    default=Value(in_person_distance),
                    output_field=FloatField(),
                )
            )
            nearby_filter = Q(joined=True) | Q(created_by=self.request.user) | Q(distance__lte=F("preferred_distance"))
            qs = qs.filter(nearby_filter)
            self.nearby_filter_active = True
        return qs

    def get_context_data(self, **kwargs):
        # Auto-remove nearby filter when no results exist but the search term has results without distance constraint
        nearby_filter_auto_removed = None
        if getattr(self, "nearby_filter_active", False) and not self.object_list.exists():
            query = self.request.GET.get("query", "")
            if query:
                base_qs = getattr(self, "_base_qs", None)
                if base_qs is not None:
                    fallback_qs = AuctionFilter({"query": query}, queryset=base_qs).qs
                    if fallback_qs.exists():
                        self.object_list = fallback_qs
                        self.nearby_filter_active = False
                        nearby_filter_auto_removed = "No nearby auctions match your search \u2014 showing all results."
        context = super().get_context_data(**kwargs)
        context["hide_google_login"] = True
        if not self.object_list.exists():
            context["no_results"] = (
                f"<span class='text-danger'>No auctions found.</span>  This only searches club auctions, if you're looking for {settings.WEBSITE_FOCUS} to buy, check out <a href='/lots/'>the list of lots for sale</a>"
            )
        context["nearby_filter_auto_removed"] = nearby_filter_auto_removed
        context["is_htmx"] = bool(self.request.headers.get("HX-Request"))
        context["show_new_auction_button"] = True
        if self.request.user.is_authenticated and not self.request.user.userdata.can_create_club_auctions:
            context["show_new_auction_button"] = False
        if not self.request.user.is_authenticated and not settings.ALLOW_USERS_TO_CREATE_AUCTIONS:
            context["show_new_auction_button"] = False
        if self.request.user.is_superuser:
            context["show_new_auction_button"] = True
        context["nearby_filter_active"] = getattr(self, "nearby_filter_active", False)
        user_has_location = getattr(self, "_user_has_location", False)
        context["user_has_location"] = user_has_location
        if user_has_location and self.request.user.is_authenticated:
            try:
                ud = self.request.user.userdata
                unit = ud.distance_unit or "miles"
                online_d = ud.email_me_about_new_auctions_distance or 100
                in_person_d = ud.email_me_about_new_in_person_auctions_distance or 100
                if unit == "km":
                    online_d = round(online_d * MILES_TO_KM)
                    in_person_d = round(in_person_d * MILES_TO_KM)
                context["online_distance"] = online_d
                context["in_person_distance"] = in_person_d
                context["distance_unit"] = unit
            except Exception:
                context["user_has_location"] = False
        return context

    def get_table(self, **kwargs):
        return self.table_class(self.get_table_data(), request=self.request, **kwargs)


class Leaderboard(ListView):
    model = UserData
    template_name = "leaderboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["total_lots"] = UserData.objects.filter(rank_total_lots__isnull=False).order_by("rank_total_lots")
        context["unique_species"] = UserData.objects.filter(number_unique_species__isnull=False).order_by(
            "rank_unique_species"
        )
        # context['total_spent'] = UserData.objects.filter(rank_total_spent__isnull=False).order_by('rank_total_spent')
        context["total_bids"] = UserData.objects.filter(rank_total_bids__isnull=False).order_by("rank_total_bids")
        return context


class AllLots(LotListView, AuctionViewMixin):
    """Show all lots"""

    rewrite_url = (
        # use JS to rewrite the shown URL.  This is used only for auctions.
        None
    )
    auction = None
    allow_non_admins = True

    def render_to_response(self, context, **response_kwargs):
        """override the default just to add a cookie -- this will allow us to save ordering for subsequent views"""
        response = super().render_to_response(context, **response_kwargs)
        if hasattr(self, "ordering"):
            response.set_cookie("lot_order", self.ordering)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        data = self.request.GET.copy()
        can_show_unloved_tip = True
        self.ordering = ""  # default ordering is set in LotFilter.__init__
        # I don't love having this in two places, but it seems necessary
        if self.request.GET.get("page"):
            del data["page"]  # required for pagination to work
        if "order" in data:
            self.ordering = data["order"]
        else:
            if "lot_order" in self.request.COOKIES:
                data["order"] = self.request.COOKIES["lot_order"]
                self.ordering = data["order"]
        if self.ordering == "unloved":
            can_show_unloved_tip = False
            if randint(1, 10) > 9:
                # we need a gentle nudge to remind people not to ALWAYS sort by least popular
                context["search_button_tooltip"] = "Sorting by least popular"
        if not context["auction"]:
            context["auction"] = self.auction
        else:
            self.auction = context["auction"]
        if self.auction:
            context["is_auction_admin"] = self.is_auction_admin
            if self.auction.minutes_to_end < 1440 and self.auction.minutes_to_end > 0 and can_show_unloved_tip:
                context["search_button_tooltip"] = "Try sorting by least popular to find deals!"
        if self.rewrite_url:
            if "auction" not in data and "q" not in data:
                context["rewrite_url"] = self.rewrite_url
        if "q" in data:
            if data["q"]:
                user = None
                if self.request.user.is_authenticated:
                    user = self.request.user
                SearchHistory.objects.create(user=user, search=data["q"], auction=self.auction)
        context["lot_view_type"] = "all"
        context["filter"] = LotFilter(
            data,
            queryset=self.get_queryset(),
            request=self.request,
            ignore=True,
            regardingAuction=self.auction,
        )
        # LotFilter hides lots posted in the last 20 minutes from non-owners. When those are the
        # only lots in the auction the list looks empty, so flag it and let the template explain the
        # short wait instead of showing a bare "No lots found". Superusers and in-person auctions
        # don't hide new lots (see LotFilter.qs), so there's nothing to explain there.
        context["recently_added_lots_hidden"] = False
        if self.auction and self.auction.is_online and not self.request.user.is_superuser:
            recent_lots = Lot.objects.filter(
                auction=self.auction,
                is_deleted=False,
                banned=False,
                date_posted__gte=timezone.now() - timedelta(minutes=20),
            )
            if self.request.user.is_authenticated:
                recent_lots = recent_lots.exclude(user=self.request.user)
            context["recently_added_lots_hidden"] = recent_lots.exists()
        context["hide_google_login"] = True
        return context


class BlogPostView(DetailView):
    """Render a blog post"""

    model = BlogPost
    template_name = "blog_post.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        blogpost = self.get_object()
        # this is to allow the chart# syntax
        context["formatted_contents"] = re.sub(r"chart\d", r"<canvas id=\g<0>></canvas>", blogpost.body_rendered)
        return context


class PrivacyPolicyView(BlogPostView):
    """The privacy policy at a stable, obvious path.

    Same content as /blog/privacy/ (one BlogPost, seeded by migration), rendered here rather than
    redirected: the app opens this URL inside the signed-out signup WebView against an allow-list of
    exactly the paths /api/mobile/config/ hands it, so a redirect elsewhere would bounce the user out
    to the system browser mid-signup. Apple requires a privacy policy linked from inside the app, and
    Google Play's data-deletion policy wants a URL — both point here.
    """

    def get_object(self, queryset=None):
        return get_object_or_404(BlogPost, slug=PRIVACY_POLICY_SLUG)


class UnsubscribeView(TemplateView):
    """
    Match a UUID in the URL to a UserData, and unsubscribe that user
    """

    template_name = "unsubscribe.html"

    def get_context_data(self, **kwargs):
        userData = UserData.objects.filter(unsubscribe_link=kwargs["slug"]).first()
        if not userData:
            raise Http404
        else:
            userData.unsubscribe_from_all()
        context = super().get_context_data(**kwargs)
        return context
