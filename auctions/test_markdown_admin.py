"""The markdown editor on the admin's blog post and FAQ pages.

These pages render django-markdownfield's widget. The site used to override its template, and when
the package renamed the widget's context (``options_id`` became ``config_id``) the stale override
turned every change page into a 500. The package's own template and script now do the work.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from auctions.models import FAQ, BlogPost


class MarkdownAdminEditorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser("mdadmin", "mdadmin@example.com", "pw")
        cls.post = BlogPost.objects.create(title="Multiple locations", body="Some *markdown*.")
        cls.faq = FAQ.objects.create(category_text="Help", question="How?", answer="Like **this**.")

    def setUp(self):
        self.client.force_login(self.superuser)

    def assert_editor_renders(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-markdownfield=")
        self.assertContains(response, "markdownfield/markdownfield.")

    def test_blog_post_change_page_renders_the_editor(self):
        self.assert_editor_renders(reverse("admin:auctions_blogpost_change", args=[self.post.pk]))

    def test_blog_post_add_page_renders_the_editor(self):
        self.assert_editor_renders(reverse("admin:auctions_blogpost_add"))

    def test_faq_change_page_renders_the_editor(self):
        self.assert_editor_renders(reverse("admin:auctions_faq_change", args=[self.faq.pk]))
