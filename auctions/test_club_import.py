"""Phase 8: importing a curated club list, and never publishing anything by accident.

Two rules are what these tests exist to hold, because both are the kind that decay silently:

* **Nothing imported is ever published.**  Every club created lands at ``PROSPECT``, which is the
  map gate.  A regression here would put researched guesses on a map this site treats as
  authoritative.
* **What a person typed is never overwritten.**  A CSV fills blanks and nothing else.  The row came
  from outside; the club row was typed by somebody who knew.

See auctions/club_import.py.
"""

import io

from django.test import TestCase

from auctions.club_import import (
    CSV_COLUMNS,
    ImportedClub,
    domain_of,
    find_existing,
    ingest,
    is_a_club_host,
    read_csv,
)
from auctions.models import Club


def csv_text(rows, columns=CSV_COLUMNS):
    """A CSV as a spreadsheet would export it, so the tests read like the file does."""
    out = io.StringIO()
    out.write(",".join(columns) + "\n")
    for row in rows:
        out.write(",".join(row.get(column, "") for column in columns) + "\n")
    return io.StringIO(out.getvalue())


class HostTests(TestCase):
    def test_a_domain_ignores_www_and_the_scheme(self):
        self.assertEqual(domain_of("https://www.Club.Example/page"), "club.example")
        self.assertEqual(domain_of("club.example"), "club.example")
        self.assertEqual(domain_of(""), "")

    def test_social_sites_are_never_a_club_homepage(self):
        for url in ("https://facebook.com/a", "https://www.youtube.com/x", "https://m.facebook.com/y"):
            self.assertFalse(is_a_club_host(url), url)
        self.assertTrue(is_a_club_host("https://gsas.example"))


class ReadCsvTests(TestCase):
    def test_it_reads_the_columns_it_is_given(self):
        rows, complaints = read_csv(
            csv_text(
                [
                    {
                        "name": "Greater Seattle Aquarium Society",
                        "homepage": "https://gsas.example",
                        "location": "Seattle WA",
                        "facebook_page": "https://facebook.com/gsas",
                        "contact_method": "email",
                        "contact_email": "hi@gsas.example",
                    }
                ]
            ),
            source="clubs.csv",
        )
        self.assertEqual(complaints, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Greater Seattle Aquarium Society")
        self.assertEqual(rows[0].contact_method, "email")
        self.assertEqual(rows[0].contact_email, "hi@gsas.example")
        self.assertIn("clubs.csv", rows[0].source)

    def test_a_name_is_the_only_thing_a_row_needs(self):
        """A club with nothing but a name is still a lead, and the hardest clubs to find have least."""
        rows, complaints = read_csv(csv_text([{"name": "Sparse Aquarium Society"}]))
        self.assertEqual(complaints, [])
        self.assertEqual(rows[0].name, "Sparse Aquarium Society")

    def test_a_bad_row_does_not_fail_the_file(self):
        """Three hundred rows and four typos should import two hundred and ninety-six clubs."""
        rows, complaints = read_csv(
            csv_text([{"name": "Good Aquarium Society"}, {"name": ""}, {"name": "Also Good Society"}])
        )
        self.assertEqual([row.name for row in rows], ["Good Aquarium Society", "Also Good Society"])
        self.assertEqual(len(complaints), 1)
        self.assertIn("Row 3", complaints[0])

    def test_an_unknown_contact_method_is_reported_not_stored(self):
        rows, complaints = read_csv(csv_text([{"name": "Odd Society", "contact_method": "carrier pigeon"}]))
        self.assertEqual(rows[0].contact_method, "")
        self.assertIn("carrier pigeon", complaints[0])

    def test_contact_method_is_not_case_sensitive(self):
        rows, complaints = read_csv(csv_text([{"name": "Shouty Society", "contact_method": "Facebook"}]))
        self.assertEqual(complaints, [])
        self.assertEqual(rows[0].contact_method, Club.FACEBOOK)

    def test_a_facebook_url_in_the_homepage_column_is_moved(self):
        """The single most likely mistake in a hand-made list, and the row is still worth having."""
        rows, complaints = read_csv(
            csv_text([{"name": "Social Only Society", "homepage": "https://facebook.com/socialonly"}])
        )
        self.assertEqual(rows[0].homepage, "")
        self.assertEqual(rows[0].facebook_page, "https://facebook.com/socialonly")
        self.assertIn("moved", complaints[0])

    def test_a_facebook_url_never_overwrites_a_real_facebook_page(self):
        rows, _complaints = read_csv(
            csv_text(
                [
                    {
                        "name": "Two Links Society",
                        "homepage": "https://facebook.com/wrong",
                        "facebook_page": "https://facebook.com/right",
                    }
                ]
            )
        )
        self.assertEqual(rows[0].facebook_page, "https://facebook.com/right")

    def test_a_file_with_no_name_column_is_refused_as_a_whole(self):
        handle = io.StringIO("club,website\nSomething,https://x.example\n")
        rows, complaints = read_csv(handle)
        self.assertEqual(rows, [])
        self.assertIn("no name column", complaints[0])

    def test_extra_columns_are_ignored_rather_than_fatal(self):
        handle = io.StringIO("name,president,homepage\nExtra Society,Someone,https://extra.example\n")
        rows, complaints = read_csv(handle)
        self.assertEqual(complaints, [])
        self.assertEqual(rows[0].homepage, "https://extra.example")


class FindExistingTests(TestCase):
    def test_the_domain_wins_over_the_name(self):
        club = Club.objects.create(name="Aquarium Society of Virginia", homepage="https://asv.example")
        found = ImportedClub(name="The Virginia Aquarium Society, Inc.", homepage="https://www.asv.example/home")
        self.assertEqual(find_existing(found, [club]), club)

    def test_a_close_name_with_no_domain_still_matches(self):
        club = Club.objects.create(name="Greater Seattle Aquarium Society", abbreviation="GSAS")
        self.assertEqual(find_existing(ImportedClub(name="GSAS"), [club]), club)

    def test_two_different_clubs_do_not_match(self):
        """Both derive the abbreviation BAS; see ClubNameMatchingTests for why that used to merge."""
        club = Club.objects.create(name="Boston Aquarium Society")
        self.assertIsNone(find_existing(ImportedClub(name="Bristol Aquarium Society"), [club]))


class IngestTests(TestCase):
    def test_everything_imported_lands_as_a_prospect(self):
        """The map gate. If this ever fails, researched guesses are on the public map."""
        report = ingest([ImportedClub(name="New Aquarium Society", homepage="https://new.example")], source="test")
        self.assertEqual(len(report.created), 1)
        self.assertEqual(report.created[0].outreach_stage, Club.PROSPECT)
        self.assertEqual(Club.objects.listed().count(), 0)

    def test_it_says_where_a_club_came_from(self):
        report = ingest([ImportedClub(name="Traced Aquarium Society")], source="clubs.csv")
        self.assertIn("clubs.csv", report.created[0].notes)

    def test_the_contact_details_are_carried_across(self):
        report = ingest(
            [ImportedClub(name="Reachable Society", contact_method=Club.WEBFORM, contact_email="a@b.example")],
            source="test",
        )
        self.assertEqual(report.created[0].contact_method, Club.WEBFORM)
        self.assertEqual(report.created[0].contact_email, "a@b.example")

    def test_a_blank_field_on_a_known_club_is_filled_in(self):
        ingest([ImportedClub(name="Known Aquarium Society", homepage="https://known.example")], source="test")
        club = Club.objects.get(name="Known Aquarium Society")
        club.homepage = ""
        club.save()
        report = ingest([ImportedClub(name="Known Aquarium Society", homepage="https://known.example")], source="test")
        self.assertEqual(len(report.updated), 1)
        club.refresh_from_db()
        self.assertEqual(club.homepage, "https://known.example")

    def test_what_a_person_typed_is_never_overwritten(self):
        club = Club.objects.create(name="Known Aquarium Society", homepage="https://known.example")
        ingest([ImportedClub(name="Known Aquarium Society", homepage="https://wrong.example")], source="test")
        club.refresh_from_db()
        self.assertEqual(club.homepage, "https://known.example")

    def test_a_contact_method_somebody_set_is_never_overwritten(self):
        club = Club.objects.create(name="Settled Aquarium Society", contact_method=Club.EMAIL)
        ingest([ImportedClub(name="Settled Aquarium Society", contact_method=Club.FACEBOOK)], source="test")
        club.refresh_from_db()
        self.assertEqual(club.contact_method, Club.EMAIL)

    def test_running_it_twice_creates_one_club(self):
        """Re-importing a corrected file has to be safe, or nobody will correct the file."""
        found = [ImportedClub(name="Idempotent Aquarium Society", homepage="https://idem.example")]
        ingest(found, source="test")
        report = ingest(found, source="test")
        self.assertEqual(report.created, [])
        self.assertEqual(Club.objects.filter(name="Idempotent Aquarium Society").count(), 1)
