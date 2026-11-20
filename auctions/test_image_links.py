"""A picture added by link is fetched once and refused unless it comes back as one (:mod:`auctions.image_links`)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from auctions import image_links
from auctions.forms import CreateImageForm

PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 0))]
PRIVATE = [(2, 1, 6, "", ("10.0.0.5", 0))]


def answer(status=200, content_type="image/jpeg", body=b"\xff\xd8\xff", location="", length=None):
    response = MagicMock()
    response.status = status
    response.headers = {"Content-Type": content_type, "Location": location}
    if length is not None:
        response.headers["Content-Length"] = str(length)
    chunks = [body] if isinstance(body, bytes) else list(body)
    response.read1.side_effect = [*chunks, b""]
    return response


@override_settings(CHECK_IMAGE_LINKS=True)
class ImageLinkTests(TestCase):
    url = "https://pictures.example/fish.jpg"

    def check(self, *responses, addresses=PUBLIC):
        with (
            patch("auctions.image_links.socket.getaddrinfo", return_value=addresses) as lookup,
            patch("auctions.image_links._open", side_effect=list(responses)) as get,
        ):
            form = CreateImageForm(data={"url": self.url, "image_source": "RANDOM"})
            valid = form.is_valid()
        self.lookups = lookup.call_count
        return valid, form, get

    def test_a_picture_is_accepted(self):
        valid, _form, get = self.check(answer())
        self.assertTrue(valid)
        url, address, headers, timeout = get.call_args.args
        self.assertEqual(address, "93.184.216.34")
        self.assertIn("Referer", headers)
        self.assertLessEqual(timeout, image_links.TIMEOUT)
        self.assertEqual(self.lookups, 1)

    def test_a_page_in_place_of_a_picture_is_refused(self):
        valid, form, _get = self.check(answer(content_type="text/html"))
        self.assertFalse(valid)
        self.assertIn(image_links.NOT_A_PICTURE, form.errors["url"])

    def test_markup_labelled_as_a_picture_is_refused(self):
        self.assertFalse(self.check(answer(body=b"<html>blocked</html>"))[0])

    def test_a_refusal_is_refused(self):
        self.assertFalse(self.check(answer(status=403))[0])

    def test_a_redirect_to_a_placeholder_is_refused(self):
        valid, _form, get = self.check(answer(status=302, location="https://pictures.example/no-hotlinking.png"))
        self.assertFalse(valid)
        self.assertEqual(get.call_count, 1)

    def test_a_redirect_to_the_same_file_is_followed_and_each_hop_vetted(self):
        self.url = "http://pictures.example/fish.jpg"
        valid, _form, get = self.check(answer(status=301, location="https://pictures.example/fish.jpg"), answer())
        self.assertTrue(valid)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(self.lookups, 2)

    def test_a_private_address_is_never_fetched(self):
        valid, _form, get = self.check(answer(), addresses=PRIVATE)
        self.assertFalse(valid)
        get.assert_not_called()

    def test_a_declared_huge_file_is_refused(self):
        valid, form, _get = self.check(answer(length=image_links.MAX_BYTES + 1))
        self.assertFalse(valid)
        self.assertIn(image_links.TOO_BIG, form.errors["url"])

    def test_only_the_first_few_kb_are_read(self):
        big = answer(body=[b"\xff" * 1024] * 15000)
        valid, _form, _get = self.check(big)
        self.assertTrue(valid)
        self.assertLessEqual(big.read1.call_count, image_links.ENOUGH_BYTES // 1024)

    def test_a_slow_drip_stops_at_the_deadline(self):
        clock = iter([0, 0, 1])
        drip = answer(body=[b"\xff"] * 10000)
        with patch("auctions.image_links.time.monotonic", side_effect=lambda: next(clock, image_links.DEADLINE + 1)):
            valid, _form, _get = self.check(drip)
        self.assertFalse(valid)
        self.assertEqual(drip.read1.call_count, 1)

    def test_the_lot_form_says_why(self):
        from auctions.forms import validate_image_url

        with (
            patch("auctions.image_links.socket.getaddrinfo", return_value=PUBLIC),
            patch("auctions.image_links._open", return_value=answer(status=404)),
        ):
            with self.assertRaises(Exception) as raised:
                validate_image_url(self.url)
        self.assertEqual(raised.exception.code, "image_link")


class PinnedConnectionTests(TestCase):
    """The connection goes to the vetted address, never to a second lookup of the name."""

    def test_https_connects_to_the_address_with_the_name_for_sni_and_host(self):
        with patch("auctions.image_links.urllib3.HTTPSConnectionPool") as pool:
            image_links._open("https://pictures.example/a/fish.jpg?x=1", "93.184.216.34", {"Accept": "image/*"}, 3)
        args, kwargs = pool.call_args
        self.assertEqual(args[0], "93.184.216.34")
        self.assertEqual(kwargs["server_hostname"], "pictures.example")
        self.assertEqual(kwargs["assert_hostname"], "pictures.example")
        self.assertEqual(kwargs["cert_reqs"], "CERT_REQUIRED")
        get = pool.return_value.urlopen.call_args
        self.assertEqual(get.args[1], "/a/fish.jpg?x=1")
        self.assertEqual(get.kwargs["headers"]["Host"], "pictures.example")
        self.assertFalse(get.kwargs["redirect"])

    def test_http_keeps_a_non_default_port_in_host(self):
        with patch("auctions.image_links.urllib3.HTTPConnectionPool") as pool:
            image_links._open("http://pictures.example:8080/fish.jpg", "93.184.216.34", {}, 3)
        self.assertEqual(pool.call_args.args[0], "93.184.216.34")
        self.assertEqual(pool.call_args.kwargs["port"], 8080)
        self.assertEqual(pool.return_value.urlopen.call_args.kwargs["headers"]["Host"], "pictures.example:8080")

    def test_rebinding_after_the_check_is_never_looked_up(self):
        """A name that answers public first and private second is fetched only from the first answer."""
        answers = iter([PUBLIC, PRIVATE])
        with (
            override_settings(CHECK_IMAGE_LINKS=True),
            patch("auctions.image_links.socket.getaddrinfo", side_effect=lambda *a, **k: next(answers)),
            patch("auctions.image_links._open", return_value=answer()) as get,
        ):
            self.assertEqual(image_links.problem("https://pictures.example/fish.jpg"), "")
        self.assertEqual(get.call_args.args[1], "93.184.216.34")


@override_settings(CHECK_IMAGE_LINKS=True)
class UnchangedLinkTests(TestCase):
    """An edit form posting back the link it already had doesn't fetch it again."""

    url = "https://pictures.example/fish.jpg"

    def fetches(self, form):
        with (
            patch("auctions.image_links.socket.getaddrinfo", return_value=PUBLIC),
            patch("auctions.image_links._open", return_value=answer()) as get,
        ):
            form.is_valid()
        return get.call_count

    def test_an_image_edit_with_the_same_link_is_not_fetched(self):
        from auctions.models import LotImage

        image = LotImage(pk=1, url=self.url, image_source="RANDOM")
        data = {"url": self.url, "image_source": "RANDOM"}
        self.assertEqual(self.fetches(CreateImageForm(data=data, instance=image)), 0)
        image.url = "https://pictures.example/old.jpg"
        self.assertEqual(self.fetches(CreateImageForm(data=data, instance=image)), 1)

    def test_a_speaker_edit_with_the_same_link_is_not_fetched(self):
        from auctions.forms import SpeakerForm
        from auctions.models import Speaker

        speaker = Speaker(pk=1, name="A speaker", url=self.url)
        data = {"name": "A speaker", "url": self.url}
        self.assertEqual(self.fetches(SpeakerForm(data=data, instance=speaker)), 0)
        self.assertEqual(self.fetches(SpeakerForm(data={**data, "location": "Somewhere"})), 1)

    def test_the_lot_view_skips_an_unchanged_link(self):
        from auctions.forms import validate_image_url

        with patch("auctions.image_links.problem") as problem:
            validate_image_url(self.url, fetch=False)
        problem.assert_not_called()
