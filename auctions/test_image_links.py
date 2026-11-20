"""A picture added by link is fetched once and refused unless it comes back as one (:mod:`auctions.image_links`)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from auctions import image_links
from auctions.forms import CreateImageForm

PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 0))]
PRIVATE = [(2, 1, 6, "", ("10.0.0.5", 0))]


def answer(status=200, content_type="image/jpeg", body=b"\xff\xd8\xff", location=""):
    response = MagicMock()
    response.status_code = status
    response.is_redirect = status in (301, 302, 303, 307, 308)
    response.headers = {"Content-Type": content_type, "Location": location}
    response.iter_content.return_value = [body]
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


@override_settings(CHECK_IMAGE_LINKS=True)
class ImageLinkTests(TestCase):
    url = "https://pictures.example/fish.jpg"

    def check(self, *responses, addresses=PUBLIC):
        with (
            patch("auctions.image_links.socket.getaddrinfo", return_value=addresses),
            patch("auctions.image_links.requests.get", side_effect=list(responses)) as get,
        ):
            form = CreateImageForm(data={"url": self.url, "image_source": "RANDOM"})
            valid = form.is_valid()
        return valid, form, get

    def test_a_picture_is_accepted(self):
        valid, _form, get = self.check(answer())
        self.assertTrue(valid)
        self.assertIn("Referer", get.call_args.kwargs["headers"])
        self.assertEqual(get.call_args.kwargs["timeout"], image_links.TIMEOUT)

    def test_a_page_in_place_of_a_picture_is_refused(self):
        valid, form, _get = self.check(answer(content_type="text/html"))
        self.assertFalse(valid)
        self.assertEqual(form.errors["url"], [image_links.NOT_A_PICTURE])

    def test_a_refusal_is_refused(self):
        self.assertFalse(self.check(answer(status=403))[0])

    def test_a_redirect_to_a_placeholder_is_refused(self):
        valid, _form, get = self.check(answer(status=302, location="https://pictures.example/no-hotlinking.png"))
        self.assertFalse(valid)
        self.assertEqual(get.call_count, 1)

    def test_a_redirect_to_the_same_file_is_followed(self):
        self.url = "http://pictures.example/fish.jpg"
        valid, _form, get = self.check(answer(status=301, location="https://pictures.example/fish.jpg"), answer())
        self.assertTrue(valid)
        self.assertEqual(get.call_count, 2)

    def test_a_private_address_is_never_fetched(self):
        valid, _form, get = self.check(answer(), addresses=PRIVATE)
        self.assertFalse(valid)
        get.assert_not_called()

    def test_a_huge_file_is_refused(self):
        big = answer()
        big.iter_content.return_value = [b"x" * (1024 * 1024)] * 16
        valid, form, _get = self.check(big)
        self.assertFalse(valid)
        self.assertEqual(form.errors["url"], [image_links.TOO_BIG])

    def test_the_lot_form_says_why(self):
        from auctions.forms import validate_image_url

        with (
            patch("auctions.image_links.socket.getaddrinfo", return_value=PUBLIC),
            patch("auctions.image_links.requests.get", return_value=answer(status=404)),
        ):
            with self.assertRaises(Exception) as raised:
                validate_image_url(self.url)
        self.assertEqual(raised.exception.code, "image_link")
