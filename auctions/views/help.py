"""The public help at /help/: the index, one guide, and the sitemap that lets search engines find them.

What the guides are and how they stay complete is in :mod:`auctions.help_guides`.
"""

import datetime

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View

from auctions import help_guides
from auctions.documents.search import web_documents
from auctions.models import Auction, Club, Document, Speaker

__all__ = ["AuctionHelp", "HelpGuideView", "HelpIndexView", "SitemapView"]


class _HelpPage(TemplateView):
    """Draws the help menu the way the account pages draw theirs."""

    active_guide = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        ctx = help_guides.help_context(self.request)
        context["help"] = ctx
        context["help_nav_groups"] = help_guides.groups_for(self.active_guide)
        context["help_nav_active"] = True
        context["enable_help"] = settings.ENABLE_HELP
        return context


class HelpIndexView(_HelpPage):
    """Every guide, a search box, and a pointer at the guide for the reader's own auction."""

    template_name = "help/index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = (self.request.GET.get("q") or "").strip()
        context["query"] = query
        context["results"] = help_guides.search(query) if query else []
        context["groups"] = help_guides.shown_groups()
        ctx = context["help"]
        if ctx.auction:
            context["your_guide"] = help_guides.guide_for_auction(ctx.auction, ctx.is_admin)
        return context


class HelpGuideView(_HelpPage):
    template_name = "help/guide.html"

    def get_context_data(self, **kwargs):
        self.active_guide = kwargs["slug"]
        guide = help_guides.GUIDES.get(self.active_guide)
        if guide is None or not guide.shown:
            raise Http404
        context = super().get_context_data(**kwargs)
        context["guide"] = guide
        context["tips"] = help_guides.tips_for(guide, context["help"])
        context["online_tutorial"] = settings.ONLINE_TUTORIAL_YOUTUBE_ID
        context["online_tutorial_chapters"] = settings.ONLINE_TUTORIAL_CHAPTERS
        context["in_person_tutorial"] = settings.IN_PERSON_TUTORIAL_YOUTUBE_ID
        context["in_person_tutorial_chapters"] = settings.IN_PERSON_TUTORIAL_CHAPTERS
        context["hybrid_tutorial"] = settings.HYBRID_TUTORIAL_YOUTUBE_ID
        context["hybrid_tutorial_chapters"] = settings.HYBRID_TUTORIAL_CHAPTERS
        return context


class AuctionHelp(View):
    """/auctions/<slug>/help/, linked from old emails and the auction menu: the guide for that auction.

    Anybody may follow it -- the guides are public -- and which guide depends on whether they run it.
    """

    def get(self, request, slug):
        auction = get_object_or_404(Auction, slug=slug, is_deleted=False)
        is_admin = request.user.is_authenticated and auction.permission_check(request.user)
        guide = help_guides.guide_for_auction(auction, is_admin)
        return redirect(help_guides.guide_url(guide.slug, auction))


SITEMAP_AUCTION_DAYS = 180


class SitemapView(View):
    """/sitemap.xml: the pages worth a search engine's time: the help, the about page, the club finder
    and auction list, listed clubs, recently promoted auctions, library documents shared with search
    engines (``web`` visibility), and the speaker directory's public profiles. Never a member page, an
    invoice or a user page, and nothing robots.txt disallows.
    """

    def get(self, request):
        base = request.build_absolute_uri("/").rstrip("/")
        paths = ["/help/", *(guide.url for guide in help_guides.GUIDES.values() if guide.shown)]
        if not settings.ALLOW_SEARCH_INDEXING:
            paths = []
        else:
            paths += [
                document.get_absolute_url()
                for document in web_documents().filter(status=Document.READY).only("pk", "title", "original_name")
            ]
            if settings.ENABLE_PROMO_PAGE:
                paths.append(reverse("promo"))
            paths += [reverse("clubs"), reverse("auctions")]
            paths += [
                reverse("club_detail", kwargs={"slug": club.slug})
                for club in Club.objects.listed().only("pk", "slug").order_by("pk")
            ]
            # Only auctions the club promotes, from the last half year on: older ones are history.
            paths += [
                auction.get_absolute_url()
                for auction in Auction.objects.filter(
                    promote_this_auction=True,
                    is_deleted=False,
                    date_start__gte=timezone.now() - datetime.timedelta(days=SITEMAP_AUCTION_DAYS),
                )
                .only("pk", "slug")
                .order_by("pk")
            ]
            paths.append(reverse("speaker_list"))
            paths += [
                speaker.get_absolute_url()
                for speaker in Speaker.objects.filter(is_deleted=False, visibility=Speaker.VISIBILITY_PUBLIC)
                .only("pk", "slug")
                .order_by("pk")
            ]
        body = render_to_string("sitemap.xml", {"urls": [base + path for path in paths]})
        return HttpResponse(body, content_type="application/xml")
