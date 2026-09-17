"""The layout of ``AuctionEditForm``: what an organizer sees first, and what is behind *Advanced*.

``Auction`` has 105 fields and this form edits 43. They used to be one flat page, and it is the
biggest drop-off surface on the site: the two-field create form hands straight over from the
checklist's "Edit the rules", so the first thing a new organizer sees is four dozen settings.

So the page is split. :data:`ESSENTIAL_FIELDS` is what a first auction has to decide -- when it
runs, what it costs, who takes the cut, whether there is online bidding, which club it belongs to.
Everything else is inside one ``<details>``.

Three rules keep the split from hiding something, each with a test:

1. **Every field is in exactly one half.** ``test_auction_form_layout`` compares the built layout
   against ``Meta.fields``, so a field forgotten here fails the build rather than vanishing.
2. **A setting already in use is never hidden.** :func:`advanced_fields_in_use` compares stored
   values against the defaults and renders the section open if any has moved.
3. **A field with an error is never hidden**, or the form says "fix this" and shows nothing to fix.

``<details>`` rather than a collapse: it needs no JavaScript, is keyboard- and screen-reader-ready
without ARIA of ours, and browser find-in-page opens it, so searching for "tax" still lands on it.

Which fields belong where is a judgement, and the evidence is :mod:`auctions.field_adoption`: an
essential field nobody has ever changed belongs down here, and an advanced one most auctions change
belongs up top.
"""

from crispy_forms.bootstrap import PrependedAppendedText
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Layout, Submit
from django import forms

# What a first auction needs decided. Everything else is behind the Advanced disclosure -- written
# this way round so a new field defaults to hidden rather than to the first screen.
ESSENTIAL_FIELDS = frozenset(
    {
        "summernote_description",
        # Dates
        "lot_submission_start_date",
        "lot_submission_end_date",
        "date_start",
        "date_end",
        # Bidding
        "online_bidding",
        "date_online_bidding_starts",
        "date_online_bidding_ends",
        # What it costs
        "unsold_lot_fee",
        "lot_entry_fee",
        "registration_fee",
        "winning_bid_percent_to_club",
        "user_cut",
        # Who runs it
        "club",
        "manage_users_through_club",
        # Getting paid, and being found
        "max_lots_per_user",
        "enable_online_payments",
        "enable_square_payments",
        "invoice_payment_instructions",
        "promote_this_auction",
    }
)

ADVANCED_SUMMARY = "Advanced settings"


def build_layout(form, currency_symbol):
    """The crispy ``Layout`` for ``AuctionEditForm``, and the helper that renders it."""

    def slot(field_name, visible_element):
        """Place a field in its grid column when visible, or render the bare hidden input when the form's
        __init__ has switched its widget to HiddenInput -- so the value stays in the POST without leaving a
        blank cell. See auction_edit_form.html for the JS-toggled fields, which collapse via toggleCol().
        """
        if isinstance(form.fields[field_name].widget, forms.HiddenInput):
            return field_name
        return visible_element

    def col(field_name, width="col-md-3", **kwargs):
        return slot(field_name, Div(field_name, css_class=width, **kwargs))

    def money(field_name, width="col-lg-3"):
        return slot(field_name, PrependedAppendedText(field_name, currency_symbol, ".00", wrapper_class=width))

    def percent(field_name, width="col-lg-3"):
        return slot(field_name, PrependedAppendedText(field_name, "", "%", wrapper_class=width))

    helper = FormHelper()
    helper.form_method = "post"
    helper.form_id = "auction-form"
    helper.form_class = "form"
    helper.form_tag = True
    helper.layout = Layout(
        "summernote_description",
        HTML("<h4>Dates</h4>"),
        Div(
            col("lot_submission_start_date"),
            col("lot_submission_end_date"),
            col("date_start", label="Bidding opens"),
            col("date_end"),
            css_class="row",
        ),
        HTML("<h4>Online bidding</h4>"),
        Div(
            col("online_bidding"),
            col("date_online_bidding_starts"),
            col("date_online_bidding_ends"),
            css_class="row",
        ),
        HTML("<h4>Lot fees</h4>"),
        Div(
            money("unsold_lot_fee"),
            money("lot_entry_fee"),
            money("registration_fee"),
            percent("winning_bid_percent_to_club"),
            percent("user_cut"),
            css_class="row",
        ),
        HTML("<h4>Club</h4>"),
        Div(
            col("club", "col-md-6"),
            col("manage_users_through_club", "col-md-6"),
            css_class="row",
        ),
        HTML("<h4>Lots and payment</h4>"),
        Div(
            col("max_lots_per_user", "col-md-4"),
            col("enable_online_payments", "col-md-4"),
            col("enable_square_payments", "col-md-4"),
            col("invoice_payment_instructions", "col-md-6"),
            col("promote_this_auction", "col-md-6"),
            css_class="row",
        ),
        # {% if %} inside HTML() is a real template fragment rendered by crispy, which is where
        # `form` comes from. It has to be decided here, since whether a field was rejected isn't
        # known until validation, after __init__.
        HTML(
            '<details class="auction-advanced mb-3" {% if form.advanced_open %}open{% endif %}>'
            f'<summary class="h4 mb-3" style="cursor: pointer;">{ADVANCED_SUMMARY}</summary>'
        ),
        HTML("<h4>Lot fee discounts</h4>"),
        Div(
            col("alternate_split_mode", "col-lg-3"),
            col("alternative_split_label", "col-lg-9"),
            css_class="row",
        ),
        Div(
            percent("pre_register_lot_discount_percent"),
            money("lot_entry_fee_for_club_members"),
            money("registration_fee_for_club_members"),
            percent("winning_bid_percent_to_club_for_club_members"),
            percent("club_member_cut"),
            money("force_donation_threshold"),
            css_class="row",
        ),
        HTML("<h4>Lot permissions</h4>"),
        Div(
            col("allow_deleting_bids"),
            col("allow_additional_lots_as_donation", "col-md-4"),
            col("only_approved_sellers", "col-md-4"),
            col("only_approved_bidders", "col-md-4"),
            col("copy_users_when_copying_this_auction", "col-md-4"),
            col("use_seller_dash_lot_numbering", "col-md-4"),
            css_class="row",
        ),
        HTML("<h4>Club extras</h4>"),
        Div(
            # Check-in mode only; shown and hidden by update_club_fields() in auction_edit_form.html.
            col("allow_self_checkin", "col-md-6"),
            money("club_member_discount", "col-md-6"),
            css_class="row",
        ),
        HTML("<h4>General</h4>"),
        Div(
            col("require_phone_number"),
            col("email_users_when_invoices_ready"),
            col("add_membership_fee_to_invoices_for_expired_members"),
            col("invoice_rounding"),
            col("only_whole_dollar_bids"),
            col("minimum_bid"),
            col("auto_add_images"),
            col("message_users_when_lots_sell"),
            percent("tax", "col-md-3"),
            css_class="row",
        ),
        HTML("</details>"),
        Submit("submit", "Save", css_class="create-update-auction btn-success"),
    )
    return helper


def advanced_fields_in_use(instance, advanced_names):
    """Whether this auction has moved any hidden setting off its default.

    The risk of an *Advanced* section is hiding something somebody relies on: a field on its default is
    one nobody has decided about, and a field off it is a decision, so it stays on screen.

    Blank and NULL are both "untouched": the form writes ``""`` where several migrations wrote NULL.
    """
    if instance is None or not getattr(instance, "pk", None):
        return False
    meta = instance._meta
    for name in advanced_names:
        try:
            field = meta.get_field(name)
        except Exception:
            continue
        default = field.get_default() if field.has_default() else None
        if callable(default):
            continue
        value = getattr(instance, name, None)
        if value in (None, "") and default in (None, ""):
            continue
        try:
            if value != default:
                return True
        except TypeError:
            # A type that will not compare. Showing the field is the safe answer.
            return True
    return False
