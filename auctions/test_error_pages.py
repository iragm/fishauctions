from django.conf import settings
from django.test import Client, TestCase


class SubresourceNotFoundTests(TestCase):
    """A 404 for an icon or image must not hand a first-time visitor a second CSRF cookie."""

    def setUp(self):
        self.client = Client()  # no cookies: a browser on its first visit

    def test_favicon_404_sets_no_csrf_cookie(self):
        response = self.client.get("/favicon.ico", headers={"Sec-Fetch-Dest": "image"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "text/plain")
        self.assertNotIn(settings.CSRF_COOKIE_NAME, response.cookies)

    def test_old_browser_icon_request_sets_no_csrf_cookie(self):
        response = self.client.get("/favicon.ico", headers={"Accept": "image/webp,*/*"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(settings.CSRF_COOKIE_NAME, response.cookies)

    def test_page_token_survives_a_concurrent_favicon_404(self):
        page = self.client.get("/", headers={"Sec-Fetch-Dest": "document"})
        rendered = page.cookies[settings.CSRF_COOKIE_NAME].value
        # The favicon request left before the page's cookie arrived, so it carries none; whatever
        # it sets lands in the browser's jar after the page's cookie.
        favicon = Client().get("/favicon.ico", headers={"Sec-Fetch-Dest": "image"})
        jar = favicon.cookies.get(settings.CSRF_COOKIE_NAME, page.cookies[settings.CSRF_COOKIE_NAME])
        self.assertEqual(jar.value, rendered)

    def test_missing_page_still_renders_the_404_page(self):
        for headers in ({"Sec-Fetch-Dest": "document"}, {}):
            response = self.client.get("/no-such-page/", headers=headers)
            self.assertEqual(response.status_code, 404)
            self.assertContains(response, "template-404", status_code=404)
