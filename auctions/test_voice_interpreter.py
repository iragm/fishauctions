"""Reading what an auctioneer said as a sale (``auctions.voice_interpreter``).

No database: the interpreter is a pure function of the transcripts, the auction's numbers and the grammar.
The transcripts in ``RecordedAuctionTests`` came back from OpenAI's live transcription of a synthetic
auctioneer, so they're what the browser listener actually produces, punctuation and all.
"""

from django.test import SimpleTestCase

from auctions.voice_interpreter import Grammar, Vocabulary, interpret, number_groups, readings, tokenize

LOTS = ["41", "42", "43", "44", "45", "46", "101-1", "BOB-1"]
BIDDERS = ["1", "5", "15", "17", "50", "104", "105", "NM", "BOB"]


def vocabulary(lots=LOTS, bidders=BIDDERS, whole_dollars=True):
    return Vocabulary(lots=lots, bidders=bidders, whole_dollars=whole_dollars)


def read(heard, lot="42", vocab=None, **kwargs):
    if isinstance(heard, str):
        heard = [heard]
    return interpret(heard, vocab or vocabulary(), Grammar.from_model(None), lot=lot, **kwargs)


def said(reading):
    """The commands as "slot=value" strings, unsure ones marked with a question mark."""
    out = []
    for command in reading.commands:
        text = f"{command.slot}={command.value}" if command.value or command.slot == "lot" else command.slot
        if command.slot in ("lot", "bidder", "price") and command.confidence < 0.77:
            text += "?"
        out.append(text)
    return out


class NumberTests(SimpleTestCase):
    def groups(self, text):
        tokens = tokenize(text)
        return [group.value for group in number_groups(tokens, 0, len(tokens), Grammar.from_model(None))]

    def test_english_decides_where_one_number_ends(self):
        self.assertEqual(self.groups("twenty five"), [25])
        self.assertEqual(self.groups("five six seven"), [5, 6, 7])
        self.assertEqual(self.groups("ten fifteen"), [10, 15])
        self.assertEqual(self.groups("a hundred and ten"), [110])
        self.assertEqual(self.groups("one hundred four"), [104])

    def test_for_and_to_are_never_digits(self):
        """The app read "sold for twelve dollars" as $16. Here "for" isn't a number at all."""
        self.assertEqual(self.groups("for twelve"), [12])
        self.assertEqual(self.groups("to seventeen"), [17])

    def test_oh_is_zero_only_inside_a_number(self):
        grammar = Grammar.from_model(None)
        self.assertIn("104", readings(tokenize("one oh four"), grammar))
        self.assertEqual(said(read("oh, sold to 104 for twenty")), ["bidder=104", "price=20", "sold"])

    def test_a_seller_dash_lot_read_without_the_dash(self):
        grammar = Grammar.from_model(None)
        self.assertIn("101-1", readings(tokenize("one oh one one"), grammar))
        self.assertIn("101-1", readings(tokenize("one oh one dash one"), grammar))
        self.assertIn("bob-1", readings(tokenize("bob one"), grammar))


class CloseTests(SimpleTestCase):
    """How an auctioneer closes a lot, and what gets saved."""

    def check(self, heard, expected, **kwargs):
        with self.subTest(heard=heard):
            self.assertEqual(said(read(heard, **kwargs)), expected)

    def test_the_closes_auctioneers_say(self):
        self.check(
            "going once going twice sold to bidder one oh four for ten dollars", ["bidder=104", "price=10", "sold"]
        )
        self.check("Sold to bidder 104 for $10.", ["bidder=104", "price=10", "sold"])
        self.check("sold to 17 for 12 dollars", ["bidder=17", "price=12", "sold"])
        self.check("Sold, four dollars to number seventeen.", ["bidder=17", "price=4", "sold"])
        self.check("Sold, 17, $4.", ["bidder=17", "price=4", "sold"])
        self.check("Okay, sold! Ten dollars, one oh five.", ["bidder=105", "price=10", "sold"])
        self.check("Twenty-five, twenty-five. Sold at twenty-five to paddle fifty.", ["bidder=50", "price=25", "sold"])
        self.check("Sold to number seventeen for four", ["bidder=17", "price=4", "sold"])
        self.check("Sold to the lady in the back, number seventeen, for four dollars", ["bidder=17", "price=4", "sold"])
        self.check("bitter one oh four, ten dollars, sold", ["bidder=104", "price=10", "sold"])
        self.check("that's 15 dollars bidder 104 sold", ["bidder=104", "price=15", "sold"])
        self.check("sold four dollars seventeen", ["bidder=17", "price=4", "sold"])
        self.check("sold to one hundred four for a hundred and ten dollars", ["bidder=104", "price=110", "sold"])

    def test_soul_is_sold(self):
        """What noise made of "Sold!" in a recording of a hall."""
        self.check("ten, fifteen going once. Soul. Bidder fifty.", ["bidder=50", "price=15", "sold"])

    def test_the_original_spec_phrase(self):
        """#502's example. The app's own parser saved this at $10."""
        self.check("lot 43 sold to bidder 5 for 6 dollars", ["lot=43", "bidder=5", "price=6", "sold"])

    def test_the_price_is_the_last_bid_the_auctioneer_had(self):
        self.check(
            "Do I hear ten? Ten! Ten dollars, twelve anyone? Going once, going twice, sold to bidder 104",
            ["bidder=104", "price=10", "sold"],
        )
        self.check("five six seven eight going once going twice sold to 104", ["bidder=104", "price=8", "sold"])
        self.check(
            "I have ten, twelve? twelve? no? ten going once going twice sold to 104", ["bidder=104", "price=10", "sold"]
        )
        self.check("who'll start me at five for this one? five! sold to 104", ["bidder=104", "price=5", "sold"])

    def test_the_answer_to_an_ask_is_a_bid(self):
        self.check(
            "Do I hear ten? Ten dollars, twelve anyone? Going once. Going twice. Sold to 104",
            ["bidder=104", "price=10", "sold"],
        )

    def test_a_bidder_named_in_the_bidding_is_not_a_bid(self):
        self.check(
            "I have ten from bidder one oh four, twelve? going once going twice sold to 104",
            ["bidder=104", "price=10", "sold"],
        )

    def test_a_bid_that_sounds_like_the_one_before_it(self):
        """ "Ten, fifteen, fifty, going once": fifteen said again and heard as fifty."""
        reading = read("Ten fifteen fifty. Going once, sold. Bidder fifty.")
        self.assertEqual(said(reading), ["bidder=50", "price=50?", "sold"])
        self.assertEqual(reading.commands[1].candidates, ["15"])

    def test_a_price_from_the_bidding_waits_for_one_said_outright(self):
        self.assertEqual(read("ten, fifteen, going once, sold. Bidder fifty.").wait, 2500)
        self.assertEqual(read("sold to bidder fifty for fifteen dollars").wait, 0)

    def test_an_ask_just_before_sold_might_have_been_taken(self):
        reading = read("10, 12 anyone? sold to 104")
        self.assertEqual(said(reading), ["bidder=104", "price=10?", "sold"])
        self.assertEqual(reading.commands[1].candidates, ["12"])

    def test_a_correction_replaces_what_was_said_before_it(self):
        self.check("sold to 104, sorry, 105, for ten", ["bidder=105", "price=10", "sold"])
        self.check("sold for ten, sorry, twelve, to 104", ["bidder=104", "price=12", "sold"])

    def test_text_bidder_numbers(self):
        self.check("sold to bidder N M for 8 dollars", ["bidder=NM", "price=8", "sold"])
        self.check("sold to bob for 8", ["bidder=BOB", "price=8", "sold"])

    def test_seller_dash_lots(self):
        self.check(
            "lot one oh one dash one, who'll give me five? five, six, sold to 104",
            ["lot=101-1", "bidder=104", "price=6", "sold"],
        )
        self.check("lot 101-1 sold to 17 for $6", ["lot=101-1", "bidder=17", "price=6", "sold"])

    def test_a_close_split_across_two_transcripts(self):
        self.check(["sold to bidder 104", "for ten"], ["bidder=104", "price=10", "sold"])
        self.check(["sold", "one oh four ten dollars"], ["bidder=104", "price=10", "sold"])

    def test_a_segment_can_end_in_the_middle_of_a_phrase(self):
        """Where a recognizer ends a segment isn't a pause: "Lot | forty-three", "one oh | four"."""
        self.check(
            ["Sold to bidder one oh four for ten dollars. Lot", "Forty-three. Sold to 17 for 4"],
            ["bidder=104", "price=10", "sold"],
        )
        self.check(
            ["Lot", "Forty-three. Two, three, four. Sold to 17 for 4"], ["lot=43", "bidder=17", "price=4", "sold"]
        )
        self.check(["sold to bidder one oh", "four for ten dollars"], ["bidder=104", "price=10", "sold"])
        self.check(["Going twice, sold five dollars paddle.", "Seventeen lot"], ["bidder=17", "price=5", "sold"])
        self.check(["six, six", "seven, sold to 17"], ["bidder=17", "price=7", "sold"])
        self.check(["Going twice. Sold to bidder one oh.", "Four for ten dollars."], ["bidder=104", "price=10", "sold"])
        self.check(
            ["Going twice. Sold to bidder one.", "One oh four for ten dollars."], ["bidder=104", "price=10", "sold"]
        )
        self.check(["sold to bidder one", "oh four for ten dollars"], ["bidder=104", "price=10", "sold"])
        self.check(["Sold to bidder one", "One oh four for ten dollars."], ["bidder=104", "price=10", "sold"])

    def test_a_number_cut_off_at_a_segment_and_said_whole_in_the_next(self):
        """What live transcription does at a turn boundary: "Lot four | Forty-two"."""
        self.check(["Lot four", "Forty-two. Twelve, sold to 105"], ["lot=42", "bidder=105", "price=12", "sold"], lot="")
        self.check(
            ["Lot forty-four.", "Four, a breeding trio of guppies. Anybody? Two dollars, nobody, no sale."],
            ["lot=44", "unsold"],
            lot="",
        )
        self.check(
            ["Bidder fifty, fifteen dollars. Lot forty", "44 a breeding trio. Two dollars? Nobody. No sale."],
            ["lot=44", "unsold"],
            lot="",
        )
        self.check(
            ["Lot forty-three.", "Three. Java fern. Ten, fifteen, sold to 50"],
            ["lot=43", "bidder=50", "price=15", "sold"],
            lot="",
        )

    def test_a_split_that_is_really_two_things(self):
        self.check(["Sold to bidder seventeen.", "Four dollars."], ["bidder=17", "price=4", "sold"])
        reading = read(["Sold to bidder seventeen for four dollars.", "Five, six, seven"])
        self.assertEqual(said(reading), ["bidder=17", "price=4", "sold"])

    def test_the_lot_named_after_sold_is_the_next_one(self):
        reading = read("15 dollars bidder 104 sold lot 43")
        self.assertEqual(said(reading), ["bidder=104", "price=15", "sold"])
        self.assertEqual(reading.carry, ["lot 43"])

    def test_one_close_said_twice_is_one_sale(self):
        reading = read("sold to bidder 104 for 10 dollars sold to bidder 104 for 10 dollars")
        self.assertEqual(said(reading), ["bidder=104", "price=10", "sold"])
        self.assertEqual(reading.carry, [])

    def test_what_follows_the_close_is_the_next_lots(self):
        reading = read(
            "sold to bidder 104 for ten dollars. Lot forty-three, a bag of java moss. Two dollars? Two, three, four. "
            "Sold, four dollars to number seventeen."
        )
        self.assertEqual(said(reading), ["bidder=104", "price=10", "sold"])
        self.assertEqual(said(read(reading.carry, lot="")), ["lot=43", "bidder=17", "price=4", "sold"])


class WaitingTests(SimpleTestCase):
    """A close missing something waits for it, and says what."""

    def test_a_close_with_no_bidder_waits(self):
        reading = read("sold for twelve dollars")
        self.assertEqual(said(reading), ["price=12", "sold"])
        self.assertIsNone(reading.carry)
        self.assertEqual(reading.note, "Waiting for the bidder")

    def test_a_bidder_this_auction_doesnt_have_is_named(self):
        reading = read("sold to bidder 999 for 10 dollars")
        self.assertIsNone(reading.carry)
        self.assertEqual(reading.note, "No bidder 999 in this auction")

    def test_half_dollars_in_a_whole_dollar_auction(self):
        reading = read("sold to bidder 104 for two dollars and fifty cents")
        self.assertEqual(said(reading), ["bidder=104", "sold"])
        self.assertIsNone(reading.carry)
        self.assertEqual(reading.note, "This auction only takes whole dollars")

    def test_cents_where_the_auction_takes_them(self):
        reading = read("sold to 104 for eight dollars and fifty cents", vocab=vocabulary(whole_dollars=False))
        self.assertEqual(said(reading), ["bidder=104", "price=8.50", "sold"])

    def test_no_lot_on_the_form_and_none_announced(self):
        reading = read("sold to 104 for 10 dollars", lot="")
        self.assertIsNone(reading.carry)
        self.assertEqual(reading.note, "Waiting for the lot number")
        self.assertEqual(
            said(read("lot 44, sold to 104 for 10 dollars", lot="")), ["lot=44", "bidder=104", "price=10", "sold"]
        )

    def test_a_number_that_sounds_like_a_bidder(self):
        """Only fifty is a bidder here, so "fifteen" fills in fifty and asks."""
        reading = read("sold to fifteen for 10 dollars", vocab=vocabulary(bidders=["50", "104"]))
        self.assertEqual(said(reading), ["bidder=50?", "price=10", "sold"])


class NotASaleTests(SimpleTestCase):
    def test_ordinary_talk(self):
        for heard in (
            "a lot of interest in this one",
            "these sold for twenty last month",
            "pass it to the back please",
            "sold out of the big ones already",
        ):
            with self.subTest(heard=heard):
                self.assertEqual(said(read(heard)), [])

    def test_a_lot_announcement_only_fills_the_lot(self):
        self.assertEqual(said(read("lot forty three")), ["lot=43"])
        self.assertEqual(said(read("lot forty two")), [])

    def test_no_sale(self):
        reading = read("No bids, no sale, back to the seller.")
        self.assertEqual(said(reading), ["unsold"])
        self.assertEqual(reading.carry, ["back to the seller."])
        self.assertEqual(said(read("lot 45 anybody? no bids. no sale")), ["lot=45", "unsold"])
        self.assertEqual(said(read("pass")), ["unsold"])


class AfterASaleTests(SimpleTestCase):
    """What follows a sale voice just saved: ``previous``."""

    previous = {"lot": "42", "winner": "104", "price": "10"}

    def test_the_close_said_again_is_dropped(self):
        reading = read("sold to 104 for ten dollars, lot 43 is a nice plant", lot="43", previous=self.previous)
        self.assertEqual(said(reading), [])
        self.assertEqual(reading.keep, ["lot 43 is a nice plant"])

    def test_a_different_close_is_reported_not_sold_to_the_next_lot(self):
        reading = read("sold to 105 for ten dollars", lot="43", previous=self.previous)
        self.assertEqual(said(reading), [])
        self.assertEqual(reading.missed, [{"lot": "42", "heard": "sold to 105 for ten dollars"}])

    def test_the_next_lot_is_sold_normally(self):
        reading = read(
            "lot forty three, java moss, two dollars, three, four, sold to 17 for 4 dollars",
            lot="43",
            previous=self.previous,
        )
        self.assertEqual(said(reading), ["bidder=17", "price=4", "sold"])

    def test_a_homophone_of_the_saved_price_is_reported(self):
        """Saved at fifty; then "fifteen dollars". Somebody should look."""
        saved = {"lot": "43", "winner": "50", "price": "50"}
        reading = read(["Fifteen dollars. Lot forty-four, a breeding trio"], lot="44", previous=saved)
        self.assertEqual(reading.missed, [{"lot": "43", "heard": "Fifteen dollars."}])
        self.assertEqual(said(reading), [])

    def test_a_correction_after_the_save_is_reported(self):
        reading = read(["Sorry, twelve dollars. Lot forty-three"], lot="43", previous=self.previous)
        self.assertEqual(reading.missed, [{"lot": "42", "heard": "Sorry, twelve dollars."}])

    def test_the_price_said_again_is_set_aside(self):
        reading = read(["Ten dollars. Two, three, sold to 17 for 3"], lot="43", previous=self.previous)
        self.assertEqual(reading.missed, [])
        self.assertEqual(said(reading), ["bidder=17", "price=3", "sold"])

    def test_scratch_that_undoes_it(self):
        self.assertEqual(said(read("scratch that", lot="43", previous=self.previous)), ["undo"])
        self.assertEqual(said(read("scratch that", lot="43")), [])


class MissedTests(SimpleTestCase):
    """A close that never finished, overtaken by the next lot."""

    def test_a_lot_that_changed_hands_without_sold(self):
        """ "Sold" came back "soul" -- or wasn't said -- but a bidder was named and the next lot came up."""
        reading = read(["Ten, fifteen going once. Mold. Bidder fifty. Fifteen dollars. Lot forty-three, java fern"])
        self.assertEqual(reading.missed, [{"lot": "42", "heard": "Bidder fifty. Fifteen dollars."}])
        self.assertEqual(said(reading), ["lot=43"])

    def test_a_new_lot_announced(self):
        reading = read("sold for ten. Lot forty three, java moss, two dollars, three, four, sold to 17 for 4 dollars")
        self.assertEqual(reading.missed, [{"lot": "42", "heard": "sold for ten."}])
        self.assertEqual(said(reading), ["lot=43", "bidder=17", "price=4", "sold"])

    heard = "sold for ten. java moss, who'll give two, two dollars, three, four, five, six, sold to 17 for 6 dollars"

    def test_a_new_round_of_bidding_moves_on_down_the_queue(self):
        reading = read(self.heard, next_lot=lambda lot: "43")
        self.assertEqual([missed["lot"] for missed in reading.missed], ["42"])
        self.assertEqual(said(reading), ["lot=43", "bidder=17", "price=6", "sold"])

    def test_a_sale_nobody_knew_the_lot_of_is_still_reported(self):
        reading = read(
            "sold to 17 for 4 dollars. Lot forty four, start me at ten, ten, fifteen, sold to 50 for 15 dollars", lot=""
        )
        self.assertEqual(reading.missed, [{"lot": "", "heard": "sold to 17 for 4 dollars."}])
        self.assertEqual(said(reading), ["lot=44", "bidder=50", "price=15", "sold"])

    def test_without_a_queue_the_lot_is_emptied_until_one_is_said(self):
        reading = read(self.heard)
        self.assertEqual(said(reading)[0], "lot=")
        self.assertEqual(reading.note, "Waiting for the lot number")
        self.assertIsNone(reading.carry)
        self.assertEqual(reading.keep[0][:5], "java ")


class RecordedAuctionTests(SimpleTestCase):
    """Transcripts OpenAI returned for a synthetic auction, fed through the page's loop: each sale is saved,
    the window becomes what followed the close, and a sale is ``previous`` until the next segment is in.
    """

    LIVE = [
        "All right, folks, lot forty-two, a trio of albino bristlenose plecos. Who'll start me at five? "
        "Five dollars, I have five. Six, six and",
        "In the back, seven eight, do I hear ten? Ten, ten dollars twelve anyone? Going once, going twice, sold "
        "to bidder one oh four for ten dollars. Lot forty-three, a bag of Java moss. Two dollars, two three four, sold",
        "Four dollars to number seventeen, lot forty-four, a pair of German blue rams. Start me at ten. Ten, "
        "fifteen, twenty, twenty in the front. Twenty-five, twenty-five.",
        "Sold at twenty-five to paddle fifty, lot forty-five. Anybody? No bids, no sale, back to the seller, lot "
        "forty-six frozen brine shrimp, three dollars, three four five.",
        "Sold to one oh five, five bucks.",
    ]
    ONE_TURN = [
        "Alright folks, lot 42, a trio of albino bristlenose plecos. Who'll start me at 5? $5 I have 5. 6, 6 in "
        "the back. 7, 8, do I hear 10? 10. $10 12 anyone? Going once, going twice, sold to bidder 104 for $10. Lot "
        "43, a bag of java moss. $2? 2, 3, 4, sold! $4 to number 17. Lot 44, a pair of German blue rams, start me "
        "at 10. 10, 15, 20, 20 in the front. 25, 25, sold at 25 to paddle 50. Lot 45, anybody? No bids, no sale, "
        "back to the seller. Lot 46, frozen brine shrimp. 3 dollars, 3, 4, 5. Sold to one oh five, five bucks."
    ]
    EXPECTED = [("42", "104", "10"), ("43", "17", "4"), ("44", "50", "25"), ("45", None, None), ("46", "105", "5")]

    def run_auction(self, segments, queue):
        unsold = ["42", "43", "44", "45", "46"]
        form = {"lot": unsold[0] if queue else "", "bidder": "", "price": ""}
        window, previous, recorded = [], None, []

        def next_lot(lot):
            position = unsold.index(lot) + 1 if lot in unsold else len(unsold)
            return unsold[position] if queue and position < len(unsold) else None

        for number, segment in enumerate(segments):
            window.append(segment)
            if previous and number - previous["at"] > 1:
                previous = None
            for _ in range(5):
                reading = read(
                    window, lot=form["lot"], vocab=vocabulary(lots=list(unsold)), previous=previous, next_lot=next_lot
                )
                action = None
                for command in reading.commands:
                    if command.slot in form:
                        form[command.slot] = command.value
                    else:
                        action = command.slot
                if reading.keep is not None:
                    window = reading.keep
                if action not in ("sold", "unsold") or reading.carry is None:
                    break
                sale = (form["lot"], form["bidder"] or None, form["price"] or None)
                recorded.append(sale if action == "sold" else (form["lot"], None, None))
                previous = (
                    {"lot": sale[0], "winner": sale[1], "price": sale[2], "at": number} if action == "sold" else None
                )
                unsold.remove(form["lot"])
                form = {"lot": unsold[0] if (queue and unsold) else "", "bidder": "", "price": ""}
                window = reading.carry
        return recorded

    def test_live_transcription_with_the_lot_queue(self):
        self.assertEqual(self.run_auction(self.LIVE, queue=True), self.EXPECTED)

    def test_live_transcription_without_it(self):
        self.assertEqual(self.run_auction(self.LIVE, queue=False), self.EXPECTED)

    def test_one_long_transcript(self):
        self.assertEqual(self.run_auction(self.ONE_TURN, queue=False), self.EXPECTED)
