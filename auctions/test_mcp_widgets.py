"""Tests for the MCP-app widgets — the ``ui://`` resources a host renders instead of the JSON.

Two things here are worth more than the rest.

:meth:`BundleTests.test_the_vendored_runtime_still_ends_in_an_export` is the one that has to hold:
the whole widget layer depends on rewriting the vendored ext-apps module's trailing ``export{…}``
into a global assignment, and the failure mode when that stops matching is a blank rectangle in
somebody's chat with the error in an iframe console nobody will ever open. It fails the build
instead.

:meth:`CatalogueTests.test_every_widget_is_attached_to_a_registered_tool` is the same audit
``test_palette_skills`` runs on the skill tables: a widget pointed at a tool that has been renamed
would be published, listed, and never rendered, because nothing would ever ask for it.
"""

import json

from django.test import SimpleTestCase

from auctions import palette_actions
from auctions.mcp import protocol, tools, widgets
from auctions.models import UserAPIKey, UserData
from auctions.tests import StandardTestCase


class BundleTests(SimpleTestCase):
    """The vendored ``@modelcontextprotocol/ext-apps`` runtime and the one edit made to it."""

    def test_the_vendored_runtime_still_ends_in_an_export(self):
        bundle = widgets._bundle()
        self.assertIn("globalThis.ExtApps={", bundle)
        self.assertNotIn("\nexport{", bundle)
        self.assertFalse(bundle.rstrip().endswith("};export"), "an export statement survived the rewrite")

    def test_the_rewrite_keeps_the_two_names_the_widget_uses(self):
        bundle = widgets._bundle()
        for name in ("App:", "applyHostStyleVariables:"):
            self.assertIn(name, bundle, f"{name} is not on globalThis.ExtApps any more")


class CatalogueTests(SimpleTestCase):
    """What is published, and that it agrees with the tool registry."""

    def test_every_widget_is_attached_to_a_registered_tool(self):
        for uri, widget in widgets.WIDGETS.items():
            self.assertTrue(widget["tools"], f"{uri} is attached to nothing and can never render")
            for name in widget["tools"]:
                self.assertIn(
                    name, palette_actions.ACTIONS, f"{uri} points at {name}, which is not a registered action"
                )

    def test_resource_descriptors_are_renderable_and_self_describing(self):
        descriptors = widgets.resource_descriptors()
        self.assertEqual(len(descriptors), len(widgets.WIDGETS))
        for descriptor in descriptors:
            self.assertEqual(descriptor["mimeType"], widgets.RESOURCE_MIME_TYPE)
            self.assertTrue(descriptor["title"].strip())
            self.assertTrue(descriptor["description"].strip())
            ui = descriptor["_meta"]["ui"]
            # Explicit, because the schema's own note says host defaults vary and a card drawn
            # inside a card is the commonest way one of these looks wrong.
            self.assertIn("prefersBorder", ui)
            # No outbound connections at all: a widget asks the host, which asks us with the
            # caller's own credential. There is no second authenticated path into this API.
            self.assertEqual(ui["csp"]["connectDomains"], [])
            self.assertTrue(ui["csp"]["resourceDomains"], "lot photos would be blocked")

    def test_the_catalogue_serialises(self):
        json.dumps(widgets.resource_descriptors())

    def test_only_the_widget_tools_carry_ui_meta(self):
        by_name = {descriptor["name"]: descriptor for descriptor in tools.tool_descriptors(None)}
        for name, descriptor in by_name.items():
            if name in widgets.TOOL_WIDGETS:
                meta = descriptor["_meta"]
                self.assertEqual(meta[widgets.RESOURCE_URI_META_KEY], widgets.TOOL_WIDGETS[name])
                # Both spellings, flat and nested, because hosts read one or the other.
                self.assertEqual(meta["ui"]["resourceUri"], widgets.TOOL_WIDGETS[name])
            else:
                # Absent rather than null: it is fifty tools' worth of a key that says nothing.
                self.assertNotIn("_meta", descriptor, f"{name} has no widget but carries ui metadata")

    #: The two writes allowed to carry a widget, and why. Both of them draw the thing they just
    #: acted on rather than the thing they are about to do -- the widget is the receipt, not the
    #: button -- which is what keeps "a host may render this" from meaning "a host may run this".
    WRITES_THAT_MAY_RENDER = {
        "set_invoice_status": "the invoice it just settled is what a checkout desk needs to see",
        "send_membership_card": "the card it just emailed is better shown than described",
    }

    def test_a_widget_only_ever_decorates_a_read(self):
        """Rendering something must never be a reason to run a write.

        Everything else on the list is a lookup, which is the shape a host may render without
        asking anybody. See :data:`WRITES_THAT_MAY_RENDER` for the exceptions and their reasons.
        """
        for name in widgets.TOOL_WIDGETS:
            action = palette_actions.ACTIONS[name]
            self.assertTrue(
                tools.read_only(action) or name in self.WRITES_THAT_MAY_RENDER,
                f"{name} writes and renders a widget; say why in WRITES_THAT_MAY_RENDER if that is deliberate",
            )


class DocumentTests(SimpleTestCase):
    """The HTML itself: self-contained, and rendering the view it says it does."""

    def test_every_widget_renders_a_complete_document(self):
        for uri, widget in widgets.WIDGETS.items():
            contents = widgets.read_resource(uri)
            html = contents["text"]
            self.assertEqual(contents["mimeType"], widgets.RESOURCE_MIME_TYPE)
            self.assertTrue(html.lstrip().startswith("<!doctype html>"), uri)
            self.assertIn(f'const VIEW = "{widget["view"]}"', html)
            self.assertIn("globalThis.ExtApps", html)
            self.assertNotIn(widgets._BUNDLE_PLACEHOLDER, html, f"{uri} shipped without the runtime")

    def test_nothing_is_fetched_from_anywhere(self):
        """The iframe's CSP blocks every external request, so a widget that makes one half-loads."""
        html = widgets.read_resource("ui://auction.fish/lot")["text"]
        # The vendored bundle mentions plenty of URLs in its own strings; what matters is that the
        # document never asks the browser to go and get one.
        for tag in ("<link ", "<script src", "@import", "<iframe"):
            self.assertNotIn(tag, html.lower(), f"the widget document contains {tag!r}")

    def test_an_unknown_uri_is_not_a_document(self):
        self.assertIsNone(widgets.read_resource("ui://auction.fish/not-a-thing"))
        self.assertIsNone(widgets.read_resource(""))

    def test_a_django_template_tag_never_reaches_the_browser(self):
        """The widget is mostly JavaScript, which is full of braces. See auctions/template_lint.py.

        Checked against the rendered template rather than against the finished document: the
        vendored runtime is 330 KB of minified JavaScript and contains ``{{`` in its own right,
        which says nothing about ours.
        """
        from django.template.loader import render_to_string

        html = render_to_string("auctions/mcp/widget.html", {"view": "winners", "widget_title": "Selling now"})
        for leftover in ("{%", "{{"):
            self.assertNotIn(leftover, html, f"{leftover} survived rendering and will show as text")


class ResourceEndpointTests(StandardTestCase):
    """``resources/list`` and ``resources/read`` over the real URL, like the rest of test_mcp."""

    url = "/mcp/"

    def setUp(self):
        super().setUp()
        UserData.objects.update(use_llm_search=True)
        raw, prefix, key_hash = UserAPIKey.generate()
        self.raw_key = raw
        UserAPIKey.objects.create(user=self.user, name="test key", prefix=prefix, key_hash=key_hash, allow_writes=True)

    def rpc(self, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post(
            self.url,
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_key}",
            HTTP_MCP_PROTOCOL_VERSION=protocol.LATEST_PROTOCOL_VERSION,
        )

    def test_initialize_says_it_serves_resources(self):
        payload = json.loads(self.rpc("initialize", {"protocolVersion": protocol.LATEST_PROTOCOL_VERSION}).content)
        capabilities = payload["result"]["capabilities"]
        self.assertIn("resources", capabilities)
        # Both false and staying false: no session, so nowhere to send the notification a true
        # here would promise.
        self.assertFalse(capabilities["resources"]["subscribe"])
        self.assertFalse(capabilities["resources"]["listChanged"])

    def test_listing_the_widgets(self):
        """Every widget is listed, and the data resources share the list without displacing them.

        ``resources/list`` carries the ui:// documents *and* the two fixed me:// reads from
        ``auctions.mcp.resources``; ``test_mcp_resources`` owns the rule about what may be in
        there at all, which is that nothing concrete with a slug in it ever is.
        """
        payload = json.loads(self.rpc("resources/list").content)
        listed = {resource["uri"] for resource in payload["result"]["resources"]}
        self.assertTrue(set(widgets.WIDGETS) <= listed, set(widgets.WIDGETS) - listed)
        self.assertTrue(all(uri.startswith(("ui://", "me://")) for uri in listed), listed)

    def test_reading_one(self):
        payload = json.loads(self.rpc("resources/read", {"uri": "ui://auction.fish/lot"}).content)
        contents = payload["result"]["contents"][0]
        self.assertEqual(contents["uri"], "ui://auction.fish/lot")
        self.assertIn("globalThis.ExtApps", contents["text"])

    def test_an_unknown_uri_is_invalid_params_not_a_crash(self):
        payload = json.loads(self.rpc("resources/read", {"uri": "ui://somewhere/else"}).content)
        self.assertEqual(payload["error"]["code"], protocol.INVALID_PARAMS)

    def test_a_read_with_no_uri_says_so(self):
        payload = json.loads(self.rpc("resources/read", {}).content)
        self.assertEqual(payload["error"]["code"], protocol.INVALID_PARAMS)
        self.assertIn("uri", payload["error"]["message"])
