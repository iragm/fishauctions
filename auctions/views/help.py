"""The public help at /help/: the index, one guide, and the sitemap that lets search engines find them.

What the guides are and how they stay complete is in :mod:`auctions.help_guides`.
"""

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.views.generic import TemplateView, View

from auctions import help_guides
from auctions.models import Auction

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


class SitemapView(View):
    """/sitemap.xml: the pages worth a search engine's time, which today means the help."""

    def get(self, request):
        base = request.build_absolute_uri("/").rstrip("/")
        paths = ["/help/", *(guide.url for guide in help_guides.GUIDES.values() if guide.shown)]
        if not settings.ALLOW_SEARCH_INDEXING:
            paths = []
        body = render_to_string("sitemap.xml", {"urls": [base + path for path in paths]})
        return HttpResponse(body, content_type="application/xml")
