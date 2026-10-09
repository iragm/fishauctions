"""The OAuth consent screen, with one rule the toolkit can't know: a connection to ``/mcp/admin/``.

An admin connection reads everything on the site, so it is handed out only to a superuser, only through
a client in ``MCP_ADMIN_CLIENT_IDS`` (claude.ai's own client metadata document, whose redirect URIs
nobody else controls), and only after the consent screen has said in so many words what it is. The
toolkit would otherwise skip that screen for ``approval_prompt=auto`` whenever the same client already
holds a token -- and an everyday ``/mcp/`` connection from claude.ai is exactly that client.

Checked on the way in (GET) and again when the form comes back (POST), since the ``resource`` rides
in a hidden field. Every request to the endpoint checks again (``admin.refusal``).
"""

from __future__ import annotations

from oauth2_provider.models import get_application_model
from oauth2_provider.views import AuthorizationView

from . import admin, auth


def _names_admin(resources) -> bool:
    from urllib.parse import urlsplit

    return any(auth.is_admin_path(urlsplit(str(uri)).path) for uri in resources)


class ConsentView(AuthorizationView):
    """The toolkit's authorization view, refusing or forcing consent for an admin connection."""

    def _refused(self, client_id: str):
        application = get_application_model().objects.filter(client_id=client_id).first()
        refused = admin.client_refusal(self.request.user, client_id, application)
        if not refused:
            return None
        response = self.render_to_response(
            {"error": {"error": "This connection can't be made", "description": refused}}
        )
        response.status_code = 403
        return response

    def get(self, request, *args, **kwargs):
        if _names_admin(request.GET.getlist("resource")):
            refused = self._refused(request.GET.get("client_id", ""))
            if refused:
                return refused
            # Always the consent screen: never the toolkit's "already approved" shortcut.
            request.GET = request.GET.copy()
            request.GET.pop("approval_prompt", None)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["admin_connection"] = _names_admin(str(context.get("resource") or "").split())
        return context

    def form_valid(self, form):
        if _names_admin(str(form.cleaned_data.get("resource") or "").split()):
            refused = self._refused(form.cleaned_data.get("client_id", ""))
            if refused:
                return refused
        return super().form_valid(form)
