import datetime
import logging
import re
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import quote

# from django.core.exceptions import ValidationError
from allauth.account.forms import ResetPasswordForm, SignupForm

# from bootstrap_datepicker_plus import DateTimePickerInput
from bootstrap_datepicker_plus.widgets import (
    DateTimePickerInput,
)  # https://github.com/monim67/django-bootstrap-datepicker-plus/issues/66
from crispy_forms.bootstrap import Div, Field, PrependedAppendedText, PrependedText
from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Fieldset, Layout, Submit
from dal import autocomplete
from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.db.models import Q
from django.forms import (
    HiddenInput,
    modelform_factory,
)
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django_recaptcha.fields import ReCaptchaField
from django_recaptcha.widgets import ReCaptchaV2Invisible
from django_summernote.widgets import SummernoteWidget
from easy_thumbnails.exceptions import EasyThumbnailsError
from PIL import Image, ImageFile, UnidentifiedImageError

from .helper_functions import get_currency_symbol
from .models import (
    Auction,
    AuctionDropdown,
    AuctionTOS,
    BapAward,
    Bid,
    Category,
    ChatSubscription,
    Club,
    ClubAnnouncement,
    ClubBapCategoryOverride,
    ClubBapGenusOverride,
    ClubEvent,
    ClubMember,
    ClubMoney,
    DonationEmail,
    DonationVendor,
    Invoice,
    InvoiceAdjustment,
    Lot,
    LotImage,
    MobileDevice,
    PayPalSeller,
    PickupLocation,
    Speaker,
    SpeakerComment,
    SpeakerTopic,
    Species,
    SpeciesCommonName,
    SquareSeller,
    UserBan,
    UserData,
    UserLabelPrefs,
    VolunteerJob,
    normalize_species_name,
    sanitize_summernote_html,
)
from .services import auction_to_copy, clone_lot_values, user_can_clone_lot
from .site_setup import SINGLE_CLUB_DEFAULT_MANAGE_MODE, get_single_club
from .species_matching import (
    species_already_named,
    species_carrying_common_name,
    split_scientific_name,
    visible_species,
)
from .validators import validate_username_no_at_symbol

# Distance conversion constant
MILES_TO_KM = 1.60934

# class DateInput(forms.DateInput):
#     input_type = 'datetime-local'

logger = logging.getLogger(__name__)


def recaptcha_is_configured():
    return bool(getattr(settings, "RECAPTCHA_ENABLED", False))


def round_to_whole_dollar(value):
    """Round a Decimal currency amount to the nearest whole dollar."""
    return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)


def apply_price_input_constraints(fields, field_names, only_whole_dollar_bids):
    """Set min/step attributes for price fields based on whole-dollar setting."""
    min_value, step = ("1", "1") if only_whole_dollar_bids else ("0.01", "0.01")
    for field_name in field_names:
        fields[field_name].widget.attrs["min"] = min_value
        fields[field_name].widget.attrs["step"] = step


#: What the empty option on every species picker says.  "No species" rather than a blank line,
#: because leaving it blank is a legitimate answer -- hardware, plants, mixed bags -- and should
#: look like a choice the user made rather than one they forgot.
NO_SPECIES_LABEL = "No species"


#: The manual search that sits under the picker.  One script for the whole page however many
#: pickers are on it, so it is delegated and guarded by a flag rather than emitted per widget.
#: Results come from ``species-autocomplete`` -- the same endpoint the "strain of" field uses --
#: with ``varieties=1``, because a strain ("Blue Dream", "Halfmoon") is exactly the sort of thing
#: somebody falls back to searching for.  Picking one appends it to the ``<select>`` and selects
#: it, so the posted value is still a pk out of the Species table and validation is unchanged.
SPECIES_SEARCH_SCRIPT = """
<script>
if (!window.speciesSearchWired) {
  window.speciesSearchWired = true;
  (function () {
    var timer = null;
    function results(box) { return box.querySelector('[data-species-search-results]'); }
    function run(box) {
      var query = box.querySelector('input').value.trim();
      var list = results(box);
      if (query.length < 3) { list.innerHTML = ''; return; }
      $.getJSON('/api/species-autocomplete/', {q: query, varieties: '1'}, function (data) {
        var rows = (data && data.results) || [];
        list.innerHTML = '';
        if (!rows.length) {
          // The end of the road, so offer the way off it where there is one.  Without this an
          // auction admin looking at a fish the list has never heard of has nothing to click:
          // the gaps page is superusers only, so /species/new/ is unreachable from here.
          var empty = document.createElement('div');
          empty.className = 'list-group-item text-muted small';
          empty.textContent = 'No species found. ';
          if (box.dataset.speciesAdd) {
            var add = document.createElement('a');
            add.href = '/species/new/?lot_name=' + encodeURIComponent(query)
                     + '&next=' + encodeURIComponent(window.location.pathname);
            add.target = '_blank';
            add.rel = 'noopener';
            add.textContent = 'Add it to the list';
            empty.appendChild(add);
          }
          list.appendChild(empty);
          return;
        }
        rows.slice(0, 20).forEach(function (row) {
          var item = document.createElement('button');
          item.type = 'button';
          item.className = 'list-group-item list-group-item-action py-1 small';
          item.textContent = row.text;
          item.dataset.speciesId = row.id;
          list.appendChild(item);
        });
      });
    }
    $(document).on('input', '[data-species-search] input', function () {
      var box = this.closest('[data-species-search]');
      clearTimeout(timer);
      timer = setTimeout(function () { run(box); }, 250);
    });
    $(document).on('click', '[data-species-search-results] button', function () {
      var box = this.closest('[data-species-search]');
      var select = document.getElementById(box.dataset.speciesSearch);
      if (!select) { return; }
      if (!select.querySelector('option[value="' + this.dataset.speciesId + '"]')) {
        var option = document.createElement('option');
        option.value = this.dataset.speciesId;
        option.textContent = this.textContent;
        select.appendChild(option);
      }
      select.value = this.dataset.speciesId;
      // Marks it as a person's choice, so a later lot-name edit can't quietly replace it.
      select.dataset.userChosen = '1';
      $(select).trigger('change');
      results(box).innerHTML = '';
      box.querySelector('input').value = '';
    });
  })();
}
</script>
"""


class SpeciesSelect(forms.Select):
    """A ``<select>`` that renders only the option already chosen, plus "No species".

    The Species table has tens of thousands of rows and rendering them all into every lot form
    would add megabytes to the page.  The browser gets the current value and nothing else; the
    suggestions endpoint (``species_suggestions``) fills in a handful of options as the user types
    the lot name.

    This is a rendering trick only.  The field is still an ordinary ``ModelChoiceField`` over the
    whole table, so validation is unchanged: a posted pk that isn't a real species is rejected,
    and no amount of DOM editing gets free text into the column.

    ``searchable`` adds a search box underneath, and is what stops the picker being a dead end.
    Everything in the ``<select>`` comes from the lot *name*, so a name the matcher can't place --
    FishBase files *Labidochromis caeruleus* under "Blue streak hap", so "Yellow lab" finds
    nothing -- left the right species unreachable, for the seller and for the auction admin
    editing the lot afterwards.  Deliberately off by default: the bulk-add forms have plenty on
    them already, and there the fix is to correct one lot afterwards on a form that has it.
    """

    def __init__(self, attrs=None, choices=(), *, searchable=False, can_add=False):
        super().__init__(attrs, choices)
        self.searchable = searchable
        self.can_add = can_add

    def optgroups(self, name, value, attrs=None):
        chosen = [str(item) for item in value if item not in ("", None)]
        choices = [("", NO_SPECIES_LABEL)]
        if chosen:
            species = Species.objects.filter(pk__in=[pk for pk in chosen if pk.isdigit()]).first()
            if species:
                choices.append((str(species.pk), species.label))
        self.choices = choices
        return super().optgroups(name, value, attrs)

    def render(self, name, value, attrs=None, renderer=None):
        html = super().render(name, value, attrs, renderer)
        if not self.searchable:
            return html
        select_id = (attrs or {}).get("id") or f"id_{name}"
        add = ' data-species-add="1"' if self.can_add else ""
        return mark_safe(  # noqa: S308 - the only interpolation is an id Django built
            html
            + f'<div class="species-search mt-1" data-species-search="{escape(select_id)}"{add}>'
            + '<input type="search" class="form-control form-control-sm"'
            + ' placeholder="Not in the list?  Search every species…" autocomplete="off">'
            + '<div class="list-group mt-1" data-species-search-results></div>'
            + "</div>"
            + SPECIES_SEARCH_SCRIPT
        )


def configure_species_field(
    fields,
    auction,
    field_name="species",
    *,
    always_render=False,
    searchable=False,
    can_add=False,
    picker=True,
    dal_for=None,
):
    """Set up the scientific-name picker on a lot form, or hide it.

    Hidden rather than removed when the auction has the field turned off, so every lot form keeps
    the same field list and the templates don't have to branch.  ``clean_species_for_auction``
    is what actually stops a hidden field from being posted into.

    ``always_render`` is for the one form where the auction is chosen *in the form itself*.  There
    the picker has to exist in the DOM whatever the auction on page load says, because the user
    can switch to an auction that does use scientific names and JavaScript can only show a field
    that is already there -- the same reason ``custom_field_1`` and the custom dropdown are
    rendered unconditionally and hidden with CSS.  Nothing about validation changes: the auction
    the form ends up with still decides, in ``clean_species_for_auction``.

    ``can_add`` offers "add it to the list" when the search finds nothing, and is for forms whose
    every user is an auction admin by construction -- there is no per-user check here, so the
    caller is asserting it.  ``SpeciesCreateView`` enforces the same thing again on the way in.

    ``picker=False`` is the quick-add pages: no control at all, just a hidden input the page fills
    in from the lot name when the matcher gives exactly one answer, shown to the seller as a line
    of text under the name.  Somebody adding forty lots at a check-in table is not choosing a
    binomial forty times, and a dropdown they have to look at for every row is the thing that
    makes them stop filling it in.  Still a real field: it posts, and the pk in it is validated
    against the Species table like any other, so nothing here loosens what can be saved.
    """
    field = fields.get(field_name)
    if field is None:
        return
    field.required = False
    field.label = "Scientific name"
    field.empty_label = NO_SPECIES_LABEL
    if not always_render and (not auction or not auction.use_scientific_name):
        field.widget = HiddenInput()
        field.help_text = ""
        return
    if not picker:
        field.widget = HiddenInput(attrs={"data-species-input": "1"})
        field.help_text = ""
        return
    if dal_for is not None:
        # One search box over the whole list, and nothing filled in for you.  The seller-facing
        # forms guess a species from the lot name because the seller is not going to look one up;
        # the auction admin's lot editor is the opposite situation -- somebody is on this form
        # *because* a lot has the wrong species or none, and a guess is what they came to overrule.
        # ``varieties=1`` because a strain ("Blue Dream", "Longfin") is exactly what gets searched
        # for here.  The query string is why the URL is reversed rather than passed by name: dal
        # uses a url containing a slash as it stands and reverses anything else.
        field.widget = autocomplete.ModelSelect2(
            url=f"{reverse('species-autocomplete')}?varieties=1",
            attrs={
                "data-placeholder": "Search every species…",
                "style": "width: 100%",
                "data-species-select": "1",
            },
        )
        # Re-assigning the queryset is what rebinds widget.choices to it -- see SpeciesAdminForm,
        # where leaving it alone made re-rendering the form die inside dal.  The auction's club is
        # passed as well as the user, so a species another admin at the same club added but nobody
        # has approved yet is still pickable on that club's lots.
        field.queryset = visible_species(dal_for, getattr(auction, "club", None))
        field.help_text = "Search by scientific name, common name or strain.  Leave blank for equipment and mixed lots."
        return
    field.widget = SpeciesSelect(
        attrs={"class": "form-select species-select", "data-species-select": "1"},
        searchable=searchable,
        can_add=can_add,
    )
    field.help_text = "Suggested from the lot name.  Pick No species for equipment and mixed lots."
    if searchable:
        field.help_text += "  Search below if what you're selling isn't offered."


def note_category_chosen_by_person(instance, cleaned_data, field_name="species_category"):
    """Clear ``category_automatically_added`` when the submitted category isn't the stored one.

    That flag is the only record of *who* put a lot in its category, and ``Lot._do_save``
    re-derives the category from the species on every save while it is set.  So a person picking a
    category on a lot that has a species has to turn it off, or their choice is silently reverted
    by the next save -- which is what happened before this existed.

    Only a *change* counts.  Re-saving a form without touching the dropdown is not a decision
    about the category, and treating it as one would freeze a machine-set category the first time
    anybody edited anything else on the lot.
    """
    if instance is None or field_name not in cleaned_data:
        return
    chosen = cleaned_data.get(field_name)
    if getattr(chosen, "pk", None) != getattr(instance, f"{field_name}_id", None):
        instance.category_automatically_added = False


def clean_species_for_auction(cleaned_data, auction, field_name="species", *, derive_category=False, instance=None):
    """Ignore a posted species when the auction doesn't use scientific names.

    Belt and braces against a stale form or a hand-rolled POST: the field is hidden in that case,
    so anything arriving in it is noise rather than intent.  What is already *stored* is kept --
    turning the setting off hides the field, it does not throw the column away.  Wiping it would
    mean a club that switched the setting off to see what it did, or off and back on, lost the
    species from every lot anybody touched in between, and their labels and CSV exports stayed
    blank afterwards.

    ``derive_category`` says "this form hides its category field once a species is picked", which
    is true of the two seller-facing forms and not of the admin's lot-edit modal.  Where the field
    is hidden, whatever ``species_category`` arrives is a leftover from before the species was
    chosen rather than a choice anybody made, and the species' own category -- from the family
    FishBase records -- is the better answer.  Where an admin can still *see* the category field,
    what they put in it is left alone.

    ``instance`` is the lot being edited, when the caller has one.  It is what makes both of those
    possible: the stored species to fall back on, and the lot to record a human's category choice
    on (see :func:`note_category_chosen_by_person`).
    """
    if not auction or not auction.use_scientific_name:
        cleaned_data[field_name] = getattr(instance, field_name, None)
        note_category_chosen_by_person(instance, cleaned_data)
        return cleaned_data
    species = cleaned_data.get(field_name)
    # The one case where the category on this form is not a person's answer: the form hides the
    # picker once a species is chosen, so nobody saw the value that arrived in it.
    if derive_category and species and "species_category" in cleaned_data:
        # A cultivar inherits its parent's category; nobody is going to map every strain by hand.
        category = species.category or (species.parent.category if species.parent_id else None)
        if category and category.name != "Uncategorized":
            cleaned_data["species_category"] = category
            if instance is not None:
                instance.category_automatically_added = True
        return cleaned_data
    note_category_chosen_by_person(instance, cleaned_data)
    return cleaned_data


def add_bootstrap_classes(form):
    """Apply Bootstrap-friendly classes to visible form widgets."""
    for field in form.fields.values():
        if isinstance(field.widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple, forms.RadioSelect)):
            css_class = "form-check-input"
        elif isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
            css_class = "form-select"
        else:
            css_class = "form-control"
        existing_class = field.widget.attrs.get("class", "")
        field.widget.attrs["class"] = f"{existing_class} {css_class}".strip()


def validate_image_url(url):
    """Validate that `url` uses http/https and points to a file with an image extension.

    Raises forms.ValidationError on failure; returns the url unchanged on success.
    Extension-based validation is used instead of making an HTTP request to avoid SSRF.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        msg = "Image URL must use http or https."
        raise forms.ValidationError(msg)
    image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
    if not any(parsed.path.lower().endswith(ext) for ext in image_extensions):
        msg = (
            "URL does not appear to point to an image. Please use a URL ending in .jpg, .jpeg, .png, .gif, .webp, etc."
        )
        raise forms.ValidationError(msg)
    return url


# Exceptions that mean "the image the user gave us is unusable" (bad/unknown format,
# truncated/corrupt data, or an oversized decompression bomb) rather than "something is
# wrong with the server". These are safe to show to the uploader as a fixable problem.
#
# Note that ``UnidentifiedImageError`` is a subclass of ``OSError`` but bare ``OSError`` is
# deliberately NOT listed here: Pillow/easy_thumbnails raise plain ``OSError`` (e.g.
# ``PermissionError`` / ``[Errno 13]``, out-of-disk-space) when *writing* the resized file,
# and those are server problems that must surface as 500s so the admins get emailed rather
# than being blamed on the user's photo.
IMAGE_PROCESSING_EXCEPTIONS = (
    UnidentifiedImageError,
    Image.DecompressionBombError,
    EasyThumbnailsError,  # includes InvalidImageFormatError
    SyntaxError,
    EOFError,
)


def validate_uploaded_image(uploaded_image):
    """Confirm `uploaded_image` is a real image that Pillow can fully decode.

    This mirrors how easy_thumbnails opens the source when generating the thumbnail on
    save (a full ``load()`` with ``LOAD_TRUNCATED_IMAGES`` enabled) so we reject exactly
    the files that would otherwise blow up during thumbnailing -- but we do it here, up
    front, where we can show the uploader a friendly, actionable message instead of a 500.

    Raises ``forms.ValidationError`` for anything that isn't a usable image. The file's
    read position is reset to the start so the subsequent model save can re-read it.
    """
    try:
        uploaded_image.seek(0)
    except (AttributeError, OSError):
        pass
    previous_truncated_setting = ImageFile.LOAD_TRUNCATED_IMAGES
    try:
        # Match easy_thumbnails' tolerance so we don't reject images it would happily
        # process (and vice-versa).
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(uploaded_image) as img:
            img.load()
    except (*IMAGE_PROCESSING_EXCEPTIONS, ValueError, OSError) as e:
        # We are only *reading* the uploaded file here, so an OSError means the image
        # data itself is bad -- not a disk/permission problem (those happen on write).
        logger.info("Rejected uploaded image: %s", e)
        msg = (
            "We couldn't read that image -- it may be corrupt or in a format we don't support. "
            "Please try a different photo (JPEG, PNG, GIF, or WEBP)."
        )
        raise forms.ValidationError(msg) from e
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous_truncated_setting
        try:
            uploaded_image.seek(0)
        except (AttributeError, OSError):
            pass
    return uploaded_image


def clean_summernote(html, max_length=16383):
    """Helper function to shorten summernote fields, which can contain thousands of formatting characters"""
    html = sanitize_summernote_html(html)
    if html is None:
        return ""
    if len(html) > max_length:
        html = re.sub(r"(?!<br\s*/?>)<.*?>", "", html)[:max_length]
    return html


# The AuctionTOS fields the quick-add form edits. ``QuickAddTOS.__init__`` configures
# ``is_club_member``, which its own Meta.fields leaves out, so the form is only usable when built
# with exactly this list. Shared by the bulk-add page's formset factory and by the command palette's
# add_person action, so both build the identical form.
QUICK_ADD_TOS_FIELDS = (
    "bidder_number",
    "name",
    "email",
    "phone_number",
    "address",
    "pickup_location",
    "is_club_member",
)


class QuickAddTOS(forms.ModelForm):
    """Add a new user to an auction by filling out only the most important fields"""

    class Meta:
        model = AuctionTOS
        fields = [
            "bidder_number",
            "name",
            "email",
            "phone_number",
            "address",
            "pickup_location",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop("auction")
        self.bidder_numbers_on_this_form = kwargs.pop("bidder_numbers_on_this_form")
        super().__init__(*args, **kwargs)
        self.fields["bidder_number"].help_text = ""
        self.fields["name"].help_text = ""
        self.fields["email"].help_text = ""
        self.fields["phone_number"].help_text = ""
        self.fields["address"].help_text = ""
        self.fields["pickup_location"].queryset = self.auction.location_qs
        if not self.auction.multi_location:
            self.fields["pickup_location"].initial = self.auction.location_qs.first()
            self.fields["pickup_location"].widget = HiddenInput()
        self.fields["is_club_member"].label = self.auction.alternative_split_label
        if self.auction.alternate_split_mode != "custom":
            # Off: the alternate split doesn't apply.  Club member discount: the flag is
            # managed automatically based on club membership.
            self.fields["is_club_member"].disabled = True
            self.fields["is_club_member"].widget = HiddenInput()

    def clean(self):
        cleaned_data = super().clean()
        bidder_number = cleaned_data.get("bidder_number")
        if bidder_number:
            existing_tos = AuctionTOS.objects.filter(bidder_number=bidder_number, auction=self.auction).order_by(
                "-createdon"
            )
            pk = cleaned_data.get("pk")
            if pk:
                existing_tos = existing_tos.exclude(pk=pk)
            else:
                self.bidder_numbers_on_this_form.append(bidder_number)
            if existing_tos.count() or self.bidder_numbers_on_this_form.count(bidder_number) > 1:
                self.add_error("bidder_number", "This bidder number is already in use")
        if cleaned_data.get("email") and not cleaned_data.get("pk"):
            # duplicate email check for new users only
            existing_tos = (
                AuctionTOS.objects.filter(email=cleaned_data.get("email"), auction=self.auction)
                .order_by("-createdon")
                .first()
            )
            if existing_tos:
                self.add_error("email", "This email address is already in use")
        name = cleaned_data.get("name")
        if not name:
            self.add_error("name", "Name is required")
        # # duplicate name check for new users only
        # else:
        #     if not cleaned_data.get('pk'):
        #         existing_tos = AuctionTOS.objects.filter(name=name, auction=self.auction).first()
        #         if existing_tos:
        #             self.add_error('name', "This name is already in use, add a middle name or a number or something to make it unique")
        return cleaned_data


# The Lot fields the quick-add form edits. ``QuickAddLot.__init__`` reaches for several of these
# by name (including ``summernote_description``, which its own Meta.fields leaves out), so the form
# is only usable when built with exactly this list. Shared by the bulk-add page's formset factory
# and by the command palette's add_lot action, so both build the identical form.
QUICK_ADD_LOT_FIELDS = (
    "lot_name",
    "summernote_description",
    "species",
    "species_category",
    "i_bred_this_fish",
    "quantity",
    "donation",
    "reserve_price",
    "buy_now_price",
    "custom_checkbox",
    "custom_field_1",
    "custom_dropdown",
)


class QuickAddLot(forms.ModelForm):
    """Add a new lot by filling out only the most important fields"""

    class Meta:
        model = Lot
        fields = [
            # "custom_lot_number",
            "lot_name",
            # "summernote_description",
            "species",
            "species_category",
            "i_bred_this_fish",
            "quantity",
            "donation",
            "reserve_price",
            "buy_now_price",
            "custom_checkbox",
            "custom_field_1",
            "custom_dropdown",
        ]
        widgets = {
            # "summernote_description": SummernoteWidget(
            #     attrs={
            #         "summernote": {
            #             "width": "100%",
            #             "height": "100px",
            #             "toolbar": [],
            #         }
            #     }
            # ),
            # "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop("auction")
        # self.custom_lot_numbers_used = kwargs.pop("custom_lot_numbers_used")
        self.is_admin = kwargs.pop("is_admin")
        self.tos = kwargs.pop("tos")
        self.new_lot_count = 0
        # we need to work around the case where a user enters duplicate custom lot numbers
        super().__init__(*args, **kwargs)
        # self.fields["custom_lot_number"].help_text = ""
        self.fields["lot_name"].label = "Lot name"
        self.fields["lot_name"].widget.attrs.update({"class": "auto-image-check"})
        self.fields["lot_name"].help_text = ""
        # if not self.is_admin:
        #    self.fields["custom_lot_number"].widget = HiddenInput()
        # if not self.auction.use_categories:
        if True:  # hide category field, it's automatically set now
            self.fields["species_category"].widget = HiddenInput()
        self.fields["species_category"].label = "Category"
        self.fields["species_category"].help_text = ""
        # No picker here: see configure_species_field.  The bulk-add pages fill this in from the
        # lot name and show what they filled in as text, with a button to clear it.
        configure_species_field(self.fields, self.auction, picker=False)
        if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
            self.fields["custom_checkbox"].label = self.auction.custom_checkbox_name
        else:
            self.fields["custom_checkbox"].widget = HiddenInput()
        if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
            self.fields["custom_field_1"].label = self.auction.custom_field_1_name
            if self.auction.custom_field_1 == "required":
                self.fields["custom_field_1"].required = True
        else:
            self.fields["custom_field_1"].widget = HiddenInput()
        self.fields["i_bred_this_fish"].label = "Breeder points"
        self.fields["i_bred_this_fish"].help_text = ""
        self.fields["quantity"].help_text = ""
        self.fields["donation"].help_text = ""
        self.fields["reserve_price"].help_text = ""
        self.fields["buy_now_price"].help_text = ""
        self.fields["species_category"].initial = Category.objects.filter(name="Uncategorized").first()
        apply_price_input_constraints(
            self.fields, ("reserve_price", "buy_now_price"), self.auction.only_whole_dollar_bids
        )
        if not self.auction.use_quantity_field:
            self.fields["quantity"].initial = 1
            self.fields["quantity"].widget = HiddenInput()
            # self.fields["description"].widget = HiddenInput()
            self.fields["summernote_description"].widget = HiddenInput()
            # self.fields["custom_lot_number"].widget = HiddenInput()
        # self.fields["description"].help_text = ""
        self.fields["summernote_description"].help_text = ""
        if self.auction.reserve_price == "disable":
            self.fields["reserve_price"].widget = HiddenInput()
            self.fields["reserve_price"].initial = self.auction.minimum_bid
        if self.auction.reserve_price == "required":
            self.fields["reserve_price"].required = True
        if self.auction.buy_now == "disable":
            self.fields["buy_now_price"].widget = HiddenInput()
        if self.auction.buy_now == "required":
            self.fields["buy_now_price"].required = True
        if not self.auction.use_categories:
            self.fields["i_bred_this_fish"].widget = HiddenInput()
        if not self.auction.use_custom_checkbox_field or not self.auction.custom_checkbox_name:
            self.fields["custom_checkbox"].widget = HiddenInput()
        if self.auction.custom_field_1 == "disable" or not self.auction.custom_field_1_name:
            self.fields["custom_field_1"].widget = HiddenInput()
        else:
            if self.auction.custom_field_1 == "required":
                self.fields["custom_field_1"].required = True
        custom_dropdown_options = list(
            AuctionDropdown.objects.filter(auction=self.auction).order_by("createdon").values_list("value", flat=True)
        )
        if (
            self.auction.use_custom_dropdown_field != "disable"
            and self.auction.custom_dropdown_name
            and len(custom_dropdown_options) >= 2
        ):
            self.fields["custom_dropdown"].widget = forms.Select(
                choices=[("", "---------")] + [(value, value) for value in custom_dropdown_options]
            )
            self.fields["custom_dropdown"].label = self.auction.custom_dropdown_name
            self.fields["custom_dropdown"].required = self.auction.use_custom_dropdown_field == "required"
        else:
            self.fields["custom_dropdown"].widget = HiddenInput()
        if not self.auction.use_quantity_field:
            self.fields["quantity"].widget = HiddenInput()
        if not self.auction.use_donation_field:
            self.fields["donation"].widget = HiddenInput()
        if not self.auction.use_i_bred_this_fish_field:
            self.fields["i_bred_this_fish"].widget = HiddenInput()

    def clean(self):
        cleaned_data = super().clean()
        clean_species_for_auction(cleaned_data, self.auction, derive_category=True, instance=self.instance)
        # custom_lot_number = cleaned_data.get("custom_lot_number")
        # if custom_lot_number:
        #     existing_lots = Lot.objects.exclude(is_deleted=True).filter(
        #         custom_lot_number=custom_lot_number, auction=self.auction
        #     )
        #     lot_number = cleaned_data.get("lot_number")
        #     if lot_number:
        #         existing_lots = existing_lots.exclude(lot_number=lot_number.pk)
        #     else:
        #         self.custom_lot_numbers_used.append(custom_lot_number)
        #     if existing_lots.count() or self.custom_lot_numbers_used.count(custom_lot_number) > 1:
        #         self.add_error("custom_lot_number", "This lot number is already in use")
        if not self.is_admin:
            if self.auction.reserve_price == "disable":
                cleaned_data["reserve_price"] = self.auction.minimum_bid
            if self.auction.buy_now == "disable" and cleaned_data.get("buy_now_price"):
                cleaned_data["buy_now_price"] = None
            if (self.auction.buy_now == "require") and not cleaned_data.get("buy_now_price"):
                self.add_error("buy_now_price", "Buy Now price is required in this auction")
            if (
                self.auction.custom_field_1 == "required" and self.auction.custom_field_1_name
            ) and not cleaned_data.get("custom_field_1"):
                self.add_error("buy_now_price", "Required in this auction")
        if self.auction.only_whole_dollar_bids:
            reserve_price = cleaned_data.get("reserve_price")
            if reserve_price is not None and reserve_price != reserve_price.to_integral_value():
                self.add_error("reserve_price", "This auction only allows whole dollar amounts.")
            buy_now_price = cleaned_data.get("buy_now_price")
            if buy_now_price is not None and buy_now_price != buy_now_price.to_integral_value():
                self.add_error("buy_now_price", "This auction only allows whole dollar amounts.")
        if self.auction.use_custom_dropdown_field != "disable" and self.auction.custom_dropdown_name:
            custom_dropdown_options = list(
                AuctionDropdown.objects.filter(auction=self.auction).values_list("value", flat=True)
            )
            selected_dropdown = cleaned_data.get("custom_dropdown", "")
            if len(custom_dropdown_options) >= 2:
                if self.auction.use_custom_dropdown_field == "required" and not selected_dropdown:
                    self.add_error(
                        "custom_dropdown", f"{self.auction.custom_dropdown_name} is required in this auction"
                    )
                if selected_dropdown and selected_dropdown not in custom_dropdown_options:
                    self.add_error("custom_dropdown", "Select a valid option")
            else:
                cleaned_data["custom_dropdown"] = ""
        else:
            cleaned_data["custom_dropdown"] = ""
        # we need to make sure users can't add extra lots
        if not self.is_admin and self.auction.max_lots_per_user:
            existing_lots = self.tos.unbanned_lot_qs
            if self.auction.allow_additional_lots_as_donation:
                existing_lots = existing_lots.exclude(donation=True)
            if not cleaned_data.get("lot_number"):
                # new lots only
                total_lots = existing_lots.count() + self.new_lot_count
                if total_lots > self.auction.max_lots_per_user:
                    if self.auction.allow_additional_lots_as_donation:
                        if not cleaned_data.get("donation"):
                            self.add_error("donation", "Any additional lots need to be a donation")
                    else:
                        self.add_error("lot_name", "You can't add more lots to this auction")
                # increment counter of unsaved lots
                if self.auction.allow_additional_lots_as_donation:
                    if not cleaned_data.get("donation"):
                        self.new_lot_count += 1
                else:
                    self.new_lot_count += 1
            else:
                is_saved = Lot.objects.filter(pk=cleaned_data.get("lot_number").pk, donation=True).first()
                if is_saved and self.auction.allow_additional_lots_as_donation and not cleaned_data.get("donation"):
                    lot_count = (
                        Lot.objects.exclude(is_deleted=True)
                        .filter(
                            auctiontos_seller=self.tos,
                            donation=False,
                            banned=False,
                        )  # .exclude(pk=cleaned_data.get("lot_number").pk)
                        .count()
                    )
                    if lot_count >= self.auction.max_lots_per_user:
                        self.add_error(
                            "donation",
                            "This needs to be a donation due to the max lots per user allowed in this auction",
                        )
        return cleaned_data


def quick_add_lot_form_class():
    """The concrete quick-add lot form, with the same fields the bulk-add page builds it with.

    Use this instead of instantiating :class:`QuickAddLot` directly -- on its own it is missing
    the fields its ``__init__`` configures, and raises ``KeyError``.
    """
    return modelform_factory(Lot, form=QuickAddLot, fields=QUICK_ADD_LOT_FIELDS)


def quick_add_tos_form_class():
    """The concrete quick-add participant form, as the bulk-add page's formset builds it.

    The counterpart to :func:`quick_add_lot_form_class`, and required for the same reason:
    :class:`QuickAddTOS` configures ``is_club_member``, which its ``Meta.fields`` leaves out, so
    instantiating it directly raises ``KeyError``.
    """
    return modelform_factory(AuctionTOS, form=QuickAddTOS, fields=QUICK_ADD_TOS_FIELDS)


class TOSFormSetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        # self.auction = kwargs['auction']
        super().__init__(*args, **kwargs)
        self.form_method = "post"

        # self.layout = Layout(
        #     Div(
        #         Div('custom_lot_number',css_class='col-sm-5',),
        #         Div('lot_name',css_class='col-sm-7',),
        #         css_class='row',
        #     ),
        #     Div(
        #         Div('quantity',css_class='col-sm-4',),
        #         Div('donation',css_class='col-sm-4',),
        #         Div('i_bred_this_fish',css_class='col-sm-4',),
        #         css_class='row',
        #     ),
        # )
        # self.add_input(Submit('submit', 'Save'))
        self.template = "auctions/bulk_add_users_row.html"


class LotFormSetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        # self.auction = kwargs['auction']
        super().__init__(*args, **kwargs)
        self.form_method = "post"

        # self.layout = Layout(
        #     Div(
        #         Div('custom_lot_number',css_class='col-sm-5',),
        #         Div('lot_name',css_class='col-sm-7',),
        #         css_class='row',
        #     ),
        #     Div(
        #         Div('quantity',css_class='col-sm-4',),
        #         Div('donation',css_class='col-sm-4',),
        #         Div('i_bred_this_fish',css_class='col-sm-4',),
        #         css_class='row',
        #     ),
        # )
        # self.add_input(Submit('submit', 'Save'))
        self.template = "auctions/bulk_add_lots_row.html"


class InvoiceAdjustmentFormSetHelper(FormHelper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form_method = "post"
        self.template = "auctions/bulk_add_invoice_adjustments_row.html"
        self.layout = Layout(
            Div(
                Div("adjustment_type", css_class="col-md-4"),
                # Whole dollars only (amount is an integer field), so no ".00" suffix that
                # would imply cents can be entered.
                Div(PrependedText("amount", "$"), css_class="col-md-4"),
                Div("notes", css_class="col-md-4"),
                css_class="row",
            )
        )


class InvoiceAdjustmentForm(forms.ModelForm):
    class Meta:
        model = InvoiceAdjustment
        fields = ["adjustment_type", "amount", "notes"]
        # widgets = {
        #     'notes': forms.Textarea(attrs={'rows': 1, 'cols': 40}),
        # }

    def __init__(self, *args, **kwargs):
        self.invoice = kwargs.pop("invoice")
        result = super().__init__(*args, **kwargs)
        if not self.is_bound and not self.instance.pk:
            self.initial["amount"] = ""
        self.fields["notes"].widget.attrs = {"placeholder": "ex: membership fee"}
        self.fields["adjustment_type"].help_text = "Charge extra adds to the invoice; Discount subtracts from it."
        self.fields["amount"].help_text = "Whole dollars only"

        return result

    def clean(self):
        cleaned_data = super().clean()
        # something = cleaned_data.get("something")
        return cleaned_data


class WinnerLot(forms.Form):
    """Used to quickly set the winners on lots.  Note that this does not use forms.ModelForm"""

    # note the use of CharFields here; if we use ChoiceFields instead, we get validation errors on submit

    lot = forms.CharField(
        widget=autocomplete.Select2(
            url="lot-autocomplete",
            forward=["auction"],
            attrs={
                "data-html": True,
                "data-container-css-class": "",
            },
        )
    )
    winner = forms.CharField(
        widget=autocomplete.Select2(
            url="auctiontos-autocomplete",
            forward=["auction", "invoice"],
            attrs={
                "data-html": True,
                "data-container-css-class": "",
            },
        )
    )
    winning_price = forms.DecimalField(label="Sell price", min_value=0, required=False, decimal_places=2, max_digits=10)
    invoice = forms.CharField(label="Invoice", max_length=100)
    auction = forms.CharField(label="Auction", max_length=100)

    def __init__(self, auction, *args, **kwargs):
        self.auction_pk = auction.pk
        self.only_whole_dollar_bids = getattr(auction, "only_whole_dollar_bids", True)
        super().__init__(*args, **kwargs)
        # Get currency symbol from auction creator
        currency_symbol = auction.currency_symbol if auction else "$"
        # Show ".00" suffix only for whole-dollar auctions
        price_suffix = ".00" if self.only_whole_dollar_bids else ""
        if self.only_whole_dollar_bids:
            self.fields["winning_price"].widget.attrs["step"] = "1"
        else:
            self.fields["winning_price"].widget.attrs["step"] = "0.01"
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "lot-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "invoice",
            "auction",
            "lot",
            "winner",
            PrependedAppendedText("winning_price", currency_symbol, price_suffix),
            # Div(
            #     Div('lot',css_class='col-md-5',),
            #     Div('winner',css_class='col-md-3',),
            #     Div('winning_price',css_class='col-md-3',),
            #     css_class='row',
            # ),
            Div(
                HTML('<button type="submit" class="btn btn-success text-dark ms-2">Save</button>'),
                css_class="row",
            ),
        )
        self.fields["auction"].initial = self.auction_pk
        self.fields["auction"].widget = HiddenInput()
        self.fields["invoice"].widget = HiddenInput()
        self.fields["invoice"].initial = "True"
        self.fields["lot"].widget.attrs["autocomplete"] = "off"
        self.fields["winning_price"].widget.attrs["autocomplete"] = "off"
        self.fields["winner"].widget.attrs["autocomplete"] = "off"

    def clean_winning_price(self):
        winning_price = self.cleaned_data.get("winning_price")
        if winning_price is not None and self.only_whole_dollar_bids:
            if winning_price != winning_price.to_integral_value():
                msg = "This auction only allows whole dollar amounts."
                raise forms.ValidationError(msg)
        return winning_price

    class Meta:
        fields = [
            "auction",
            "lot",
            "winner",
            "winning_price",
        ]


class WinnerLotSimple(WinnerLot):
    """Simplified form using char fields instead of autocomplete fields"""

    lot = forms.CharField(max_length=20, required=False)
    winner = forms.CharField(max_length=20, required=False)


class WinnerLotSimpleImages(WinnerLotSimple):
    """Horizontal layout"""

    def __init__(self, auction, *args, **kwargs):
        super().__init__(auction, *args, **kwargs)
        # Get currency symbol from auction creator
        currency_symbol = auction.currency_symbol if auction else "$"
        price_suffix = ".00" if getattr(auction, "only_whole_dollar_bids", True) else ""
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "lot-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "invoice",
            "auction",
            # 'lot',
            # 'winner',
            Div(
                Div("lot", css_class="col-md-3"),
                Div(
                    PrependedAppendedText("winning_price", currency_symbol, price_suffix),
                    css_class="col-md-4",
                ),
                Div("winner", css_class="col-md-3"),
                Div(
                    HTML("""
                         <div class="btn-group mt-4" role="group" aria-label="Save">
                            <button type="submit" class="btn btn-success form-control">Save</button>
                            <div class="btn-group" role="group">
                                <button id="btnGroupDrop2" type="button" class="btn btn-success dropdown-toggle" data-bs-toggle="dropdown" aria-haspopup="true" aria-expanded="false"></button>
                                <div class="dropdown-menu" aria-labelledby="btnGroupDrop2" style="">
                                <a id='mark_unsold' class="dropdown-item" href="#">End lot unsold</a>
                                <script>
                                    $(document).ready(function() {
                                        $('#mark_unsold').click(function(event) {
                                            $('#id_winning_price').val(0);
                                            $(this).closest('form').submit();
                                        });
                                    });
                                </script>
                                <span id='make_online_bidder_winner'></span>
                                </div>
                            </div>
                            </div>
                         """),
                    css_class="col-md-2 form-group",
                ),
                css_class="row",
            ),
        )
        self.fields["auction"].initial = self.auction_pk
        self.fields["auction"].widget = HiddenInput()
        self.fields["invoice"].widget = HiddenInput()
        self.fields["invoice"].initial = "True"


class MultiAuctionTOSPrintLabelForm(forms.Form):
    print_only_unprinted = forms.BooleanField(
        required=False,
        initial=True,
        label="Only print labels that haven't been printed yet",
        help_text="Uncheck if you hate trees",
    )

    def __init__(self, *args, **kwargs):
        auctiontos = kwargs.pop("auctiontos", AuctionTOS.objects.none())
        super().__init__(*args, **kwargs)
        for tos in auctiontos:
            if tos.unprinted_labels_count == 0:
                help_text = f"All {tos.lots_count} label(s) printed"
            elif tos.unprinted_labels_count == tos.lots_count:
                help_text = f'{tos.lots_count} lot(s) added, <span class="text-warning">no label(s) printed</span>'
            else:
                help_text = f'{tos.lots_count} lot(s) added, <span class="text-warning">{tos.unprinted_labels_count}</span> label(s) unprinted'

            self.fields[f"tos_{tos.pk}"] = forms.BooleanField(
                required=False,
                label=f"{tos.bidder_number} - {tos.name}",
                help_text=help_text,
            )

        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.add_input(Submit("submit", "Print"))
        layout = [
            "print_only_unprinted",
            HTML("<h5>Check all the users you want to print labels for:</h5>"),
        ]

        # Dynamically add fields to layout
        for field_name in self.fields:
            if field_name != "print_only_unprinted":
                layout.append(Field(field_name, css_class="form-group"))

        # Set the layout to FormHelper
        self.helper.layout = Layout(*layout)


class DeleteAuctionTOS(forms.Form):
    """For deleting auctionTOS and optionally merging lots, admins only"""

    delete_lots = forms.BooleanField(required=False)
    merge_with = forms.CharField(
        widget=autocomplete.Select2(
            url="auctiontos-autocomplete",
            forward=["auction", "exclude_auctiontos"],
            attrs={
                "data-html": True,
                "data-container-css-class": "",
            },
        )
    )
    auction = forms.CharField(label="Auction", max_length=100)
    exclude_auctiontos = forms.IntegerField(required=False)

    def __init__(self, auctiontos, auction, *args, **kwargs):
        self.auction = auction
        self.auctiontos = auctiontos
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "auctiontos-delete-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "auction",
            "exclude_auctiontos",
            Div(
                "delete_lots",
                css_class="row",
            ),
            Div(
                Div(
                    "merge_with",
                    css_class="col-sm-12",
                ),
                css_class="row",
                css_id="merge_selection",
            ),
            Div(
                HTML('<a class="btn btn-secondary" href="javascript:window.history.back();">Keep this user</a>'),
                HTML('<button type="submit" class="btn btn-danger">Delete</button>'),
                css_class="modal-footer",
            ),
        )
        self.fields["auction"].widget = HiddenInput()
        self.fields["auction"].initial = self.auctiontos.auction.pk
        self.fields["exclude_auctiontos"].widget = HiddenInput()
        self.fields["exclude_auctiontos"].initial = self.auctiontos.pk
        self.fields["merge_with"].required = False
        existing_lots = self.auctiontos.unbanned_lot_count
        bought_lots = self.auctiontos.bought_lots_qs.count()
        self.lots_exist = True
        if not existing_lots and not bought_lots:
            self.lots_exist = False
            self.fields["delete_lots"].widget = HiddenInput()
        else:
            self.fields[
                "delete_lots"
            ].label = f"Also delete {existing_lots} lot(s) for this user and mark {bought_lots} lot(s) that this user won as unsold"
            self.fields[
                "delete_lots"
            ].help_text = "Uncheck if this is a duplicate user.  Lot numbers will not be changed."
            self.fields["merge_with"].label = "To keep these lots, select a user to assign them to"
        # An invoice records payment/adjustment history that a plain delete would
        # cascade away. Deleting is blocked in that case; a merge (which moves the
        # invoice, adjustments, and payments onto another user) is the only way out.
        self.invoice_exists = Invoice.objects.filter(auctiontos_user=self.auctiontos).exists()
        if self.invoice_exists:
            self.fields["delete_lots"].widget = HiddenInput()
            self.fields["delete_lots"].initial = False
            self.fields["merge_with"].label = (
                "This user has an invoice, so their payment history can't be deleted. "
                "Select another user to merge them into — their lots, adjustments, and payments move to that user."
            )

    def clean(self):
        cleaned_data = super().clean()
        delete_lots = cleaned_data.get("delete_lots")
        merge_with = cleaned_data.get("merge_with")
        if self.invoice_exists and (delete_lots or not merge_with):
            # Force the merge path; the destructive branches would erase the invoice.
            self.add_error(
                "merge_with",
                "This user has an invoice. Select another user to merge them into so their payment history is preserved.",
            )
        if not delete_lots:
            if not merge_with:
                if self.lots_exist:
                    self.add_error("merge_with", "Select a new user to preserve this user's data")
            else:
                if AuctionTOS.objects.get(pk=merge_with).auction.pk != self.auctiontos.auction.pk:
                    self.add_error("merge_with", "This shouldn't even be possible!")
                if AuctionTOS.objects.get(pk=merge_with) == self.auctiontos:
                    self.add_error("merge_with", "You can't select the user you're about to delete")
        return cleaned_data


class AuctionTOSMergeTargetForm(forms.Form):
    target = forms.CharField(
        widget=autocomplete.Select2(
            url="auctiontos-autocomplete",
            forward=["auction", "exclude_auctiontos"],
            attrs={
                "data-html": True,
                "data-container-css-class": "",
            },
        )
    )
    auction = forms.CharField(label="Auction", max_length=100)
    exclude_auctiontos = forms.IntegerField(required=False)

    def __init__(self, auctiontos, auction, *args, **kwargs):
        self.auction = auction
        self.auctiontos = auctiontos
        super().__init__(*args, **kwargs)
        self.fields["target"].label = f"Merge {self.auctiontos.name} with"
        self.fields["auction"].widget = HiddenInput()
        self.fields["auction"].initial = self.auction.pk
        self.fields["exclude_auctiontos"].widget = HiddenInput()
        self.fields["exclude_auctiontos"].initial = self.auctiontos.pk

    def clean_target(self):
        target_pk = self.cleaned_data["target"]
        try:
            target = AuctionTOS.objects.get(pk=target_pk, auction=self.auction)
        except AuctionTOS.DoesNotExist as exc:
            msg = "Select a user from this auction to keep"
            raise forms.ValidationError(msg) from exc
        if target == self.auctiontos:
            msg = "You can't select the user you're about to delete"
            raise forms.ValidationError(msg)
        return target


class AuctionTOSMergeReviewForm(forms.ModelForm):
    class Meta:
        model = AuctionTOS
        fields = ["name", "email", "phone_number", "address", "pickup_location"]

    def __init__(self, *args, auction, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pickup_location"].queryset = auction.location_qs
        add_bootstrap_classes(self)


class EditLot(forms.ModelForm):
    """Used for HTMX calls to update Lot.
    For auction admins only.
    Note that unlike AuctionTOS (which has a similar form), this form will ONLY update lots, not create them"""

    def __init__(self, user, lot, auction, *args, **kwargs):
        self.user = user
        self.auction = auction
        self.lot = lot
        super().__init__(*args, **kwargs)
        post_url = reverse("auctionlotadmin", kwargs={"pk": lot.pk})
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "lot-form"
        self.helper.form_tag = True
        # Everything an auction has turned off is a HiddenInput by the end of this method, and a
        # hidden field left in the layout still renders the grid column that wrapped it -- an
        # empty col-sm-3 is a quarter of a row of nothing.  So the layout is built at the *bottom*
        # of __init__, once the widgets are settled, and it leaves those fields out; this puts
        # their inputs back at the end of the form so the values still post.
        self.helper.render_hidden_fields = True
        # self.fields['species_category'].queryset = auction.location_qs #PickupLocation.objects.filter(auction=self.auction).order_by('name')
        # self.fields["custom_lot_number"].initial = self.lot.custom_lot_number
        self.fields["auction"].initial = self.lot.auction
        # if self.lot.label_printed:
        #     self.fields[
        #         "custom_lot_number"
        #     ].help_text = (
        #         "<span class='text-warning'>Label already printed!</span> Make sure to reprint it if you change this"
        #     )
        # else:
        #    self.fields["custom_lot_number"].help_text = "Leave blank to automatically generate"
        self.fields["lot_name"].initial = self.lot.lot_name
        # self.fields["description"].initial = self.lot.description
        self.fields["summernote_description"].initial = self.lot.summernote_description
        # self.fields['auctiontos_seller'].initial = self.lot.auctiontos_seller
        self.fields["quantity"].initial = self.lot.quantity
        self.fields["donation"].initial = self.lot.donation
        self.fields["winning_price"].initial = self.lot.winning_price
        if not self.auction.use_description:
            self.fields["summernote_description"].widget = HiddenInput()
        if not self.auction.use_categories:
            self.fields["species_category"].widget = HiddenInput()
        if not self.auction.use_quantity_field:
            self.fields["quantity"].widget = HiddenInput()
        if not self.auction.use_donation_field:
            self.fields["donation"].widget = HiddenInput()
        if not self.auction.use_i_bred_this_fish_field:
            self.fields["i_bred_this_fish"].widget = HiddenInput()
        self.fields["species_category"].initial = self.lot.species_category
        # A dal picker, and no guessing: see configure_species_field.  The "New" button beside it
        # is the way out when the list really is missing the fish -- LotAdmin, the only view that
        # renders this form, is auction admins only, which is exactly the standing
        # SpeciesCreateView asks for.
        configure_species_field(self.fields, self.auction, dal_for=self.user)
        self.fields["species"].initial = self.lot.species
        self.fields["i_bred_this_fish"].initial = self.lot.i_bred_this_fish
        self.fields["buy_now_price"].initial = self.lot.buy_now_price
        self.fields["reserve_price"].initial = self.lot.reserve_price
        self.fields["buy_now_price"].help_text = ""
        self.fields["reserve_price"].help_text = ""
        apply_price_input_constraints(
            self.fields, ("reserve_price", "buy_now_price", "winning_price"), self.auction.only_whole_dollar_bids
        )

        self.fields["custom_checkbox"].initial = self.lot.custom_checkbox
        self.fields["custom_checkbox"].help_text = ""
        self.fields["custom_field_1"].initial = self.lot.custom_field_1
        self.fields["custom_field_1"].custom_field_1 = ""
        if self.auction.use_custom_checkbox_field and self.auction.custom_checkbox_name:
            self.fields["custom_checkbox"].label = self.auction.custom_checkbox_name
        else:
            self.fields["custom_checkbox"].widget = HiddenInput()
        if self.auction.custom_field_1 != "disable" and self.auction.custom_field_1_name:
            self.fields["custom_field_1"].label = self.auction.custom_field_1_name
            if self.auction.custom_field_1 == "required":
                self.fields["custom_field_1"].required = True
        else:
            self.fields["custom_field_1"].widget = HiddenInput()
        custom_dropdown_options = list(
            AuctionDropdown.objects.filter(auction=self.auction).order_by("createdon").values_list("value", flat=True)
        )
        current_dropdown_value = self.lot.custom_dropdown
        if current_dropdown_value and current_dropdown_value not in custom_dropdown_options:
            custom_dropdown_options.append(current_dropdown_value)
        self.fields["custom_dropdown"].initial = current_dropdown_value
        if (
            self.auction.use_custom_dropdown_field != "disable"
            and self.auction.custom_dropdown_name
            and len(custom_dropdown_options) >= 2
        ):
            self.fields["custom_dropdown"].widget = forms.Select(
                choices=[("", "---------")] + [(value, value) for value in custom_dropdown_options]
            )
            self.fields["custom_dropdown"].label = self.auction.custom_dropdown_name
            self.fields["custom_dropdown"].required = self.auction.use_custom_dropdown_field == "required"
            self.fields["custom_dropdown"].help_text = ""
        else:
            self.fields["custom_dropdown"].widget = HiddenInput()
        self.fields["banned"].initial = self.lot.banned
        self.fields["auctiontos_winner"].initial = self.lot.auctiontos_winner
        # and some housekeeping on labels and help text
        self.fields["winning_price"].label = "Sell price"
        self.fields["winning_price"].help_text = ""
        self.fields["lot_name"].help_text = ""
        self.fields["species_category"].help_text = ""
        self.fields["auctiontos_winner"].label = "Winner"
        winner_help_test = ""
        if lot.high_bidder:
            winner_help_test = f"High bidder: <span class='text-warning'>{lot.high_bidder_for_admins}</span> Bid: <span class='text-warning'>${lot.high_bid}</span> {lot.auction_show_high_bidder_template}"
        self.fields["auctiontos_winner"].help_text = winner_help_test
        # self.fields['auctiontos_seller'].label = "Seller"
        # self.fields['auctiontos_seller'].help_text = ""
        self.fields["quantity"].help_text = ""
        self.fields["donation"].help_text = ""
        self.fields["i_bred_this_fish"].label = "Breeder points"
        self.fields["i_bred_this_fish"].help_text = ""

        self.helper.layout = Layout("auction", *self._layout_rows(post_url))

    def _hidden(self, field_name):
        """True when the auction has this field turned off.  See ``render_hidden_fields``."""
        return isinstance(self.fields[field_name].widget, HiddenInput)

    def _column(self, field_name, css_class, *extra):
        """A grid column for one field, or nothing at all when that field is turned off."""
        if self._hidden(field_name):
            return None
        return Div(field_name, *extra, css_class=css_class)

    @staticmethod
    def _row(*columns, css_class="row"):
        """A row of whichever columns survived.  No columns means no row, rather than an empty one."""
        kept = [column for column in columns if column is not None]
        return Div(*kept, css_class=css_class) if kept else None

    def _species_block(self):
        """The category and the scientific name, behind a Change button.

        The two of them together are one decision -- ``Species.category`` answers the category and
        the server re-derives it on save -- and on a lot that already has the right answer they are
        two full-width controls the admin scrolls past on the way to the price.  So the modal shows
        what the lot says now in one line and opens the controls when somebody wants to argue with
        it, which is the same bargain the seller's own lot form strikes (``refreshSpeciesUI`` in
        lot_form.html).  Bootstrap's collapse data-api is delegated from the document, so this
        works in a modal HTMX swapped in long after page load with no javascript of ours.
        """
        category = self._column("species_category", "col-sm-5")
        # The buttons open in a new tab on purpose: this form is an HTMX modal, and navigating away
        # from it would throw away everything else the admin has typed.  The lot name goes with
        # them, so both forms arrive half filled in, and the species can be attached to every lot
        # called that in one go.
        #
        # Two buttons because there are two different problems behind an empty picker, and the
        # commoner one is not a missing species: it is a species that is on the list under a name
        # nobody types.  Naming that one is first, because adding a second copy of it is the
        # mistake this pair of buttons exists to head off.
        lot_list = reverse("auction_lot_list", kwargs={"slug": self.auction.slug})
        buttons = HTML(
            f'<a class="btn btn-sm btn-primary mb-2" target="_blank" rel="noopener" '
            f'href="{reverse("species_name_create")}?lot_name={quote(self.lot.lot_name or "")}'
            f'&next={quote(lot_list)}">'
            '<i class="bi bi-tag"></i> Name an existing species</a> '
            f'<a class="btn btn-sm btn-primary mb-2" target="_blank" rel="noopener" '
            f'href="{reverse("species_create")}?lot_name={quote(self.lot.lot_name or "")}'
            f'&next={quote(lot_list)}">'
            '<i class="bi bi-plus-lg"></i> New species</a>'
        )
        species = self._column("species", "col-sm-7", buttons)
        fields = self._row(category, species)
        if fields is None:
            return None
        summary = []
        if category is not None:
            summary.append(f"Category: <strong>{escape(self.lot.species_category or 'Uncategorized')}</strong>")
        if species is not None:
            # full_scientific_name, never scientific_name: a strain and a cross are only themselves
            # under the name the trade uses for them.
            name = self.lot.species.full_scientific_name if self.lot.species else ""
            summary.append(f"Scientific name: <strong>{escape(name or 'none')}</strong>")
        return Div(
            HTML(
                '<div class="d-flex flex-wrap align-items-center gap-3 mb-3">'
                f'<span class="text-muted">{" &middot; ".join(summary)}</span>'
                '<button class="btn btn-sm btn-primary" type="button" '
                'data-bs-toggle="collapse" data-bs-target="#lot-species-fields" '
                'aria-expanded="false" aria-controls="lot-species-fields">Change</button>'
                "</div>"
            ),
            # Open on a re-render, because the only reason this form comes back bound is that
            # something failed validation, and an error message inside a collapsed block is an
            # error message nobody reads.
            Div(
                fields,
                css_class="col-sm-12 collapse" + (" show" if self.is_bound else ""),
                css_id="lot-species-fields",
            ),
            css_class="col-sm-12",
        )

    def _layout_rows(self, post_url):
        """The whole form, minus whatever this auction has turned off."""
        rows = (
            self._row(self._column("quantity", "col-sm-5")),
            self._row(
                self._column("lot_name", "col-sm-12"),
                self._species_block(),
                self._column("custom_field_1", "col-sm-9"),
                self._column("custom_checkbox", "col-sm-3"),
                self._column("custom_dropdown", "col-sm-3"),
                self._column("i_bred_this_fish", "col-sm-3"),
                self._column("donation", "col-sm-3"),
                self._column("buy_now_price", "col-sm-3"),
                self._column("reserve_price", "col-sm-3"),
                self._column("summernote_description", "col-sm-12"),
            ),
            self._row(
                self._column("auctiontos_winner", "col-sm-6"),
                self._column("winning_price", "col-sm-6"),
            ),
            Div(
                HTML(
                    f'<a class="btn btn-primary me-2" href="{reverse("single_lot_label", kwargs={"pk": self.lot.pk})}"><i class="bi bi-tag"></i> {"Reprint label" if self.lot.label_printed else "Print label"}</a>'
                ),
                HTML(
                    '<button type="button" class="btn btn-secondary me-auto" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(
                    f'<button hx-post="{post_url}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark ms-2">Save</button>'
                ),
                css_class="modal-footer",
            ),
        )
        return [row for row in rows if row is not None]

    class Meta:
        model = Lot
        fields = [
            "lot_name",
            # "custom_lot_number",
            "auction",
            "species",
            "species_category",
            # "description",
            "summernote_description",
            # 'auctiontos_seller',
            "quantity",
            "donation",
            "i_bred_this_fish",
            "buy_now_price",
            "reserve_price",
            "banned",
            "auctiontos_winner",
            "winning_price",
            "custom_checkbox",
            "custom_field_1",
            "custom_dropdown",
        ]
        widgets = {
            "summernote_description": SummernoteWidget(attrs={"summernote": {"width": "100%", "height": "300px"}}),
            # "description": forms.Textarea(attrs={"rows": 2}),
            # 'auctiontos_seller': autocomplete.ModelSelect2(url='auctiontos-autocomplete', forward=['auction'], attrs={'data-html': True, 'data-container-css-class': ''}),
            "auctiontos_winner": autocomplete.ModelSelect2(
                url="auctiontos-autocomplete",
                forward=["auction"],
                attrs={"data-html": True, "data-container-css-class": ""},
            ),
            "auction": HiddenInput(),
        }

    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        clean_species_for_auction(cleaned_data, auction, instance=self.instance)
        if auction:
            if not auction.permission_check(self.user):
                self.add_error("auction", "How did you even manage to change this field?")
            if auction.only_whole_dollar_bids:
                reserve_price = cleaned_data.get("reserve_price")
                if reserve_price is not None and reserve_price != reserve_price.to_integral_value():
                    self.add_error("reserve_price", "This auction only allows whole dollar amounts.")
                buy_now_price = cleaned_data.get("buy_now_price")
                if buy_now_price is not None and buy_now_price != buy_now_price.to_integral_value():
                    self.add_error("buy_now_price", "This auction only allows whole dollar amounts.")
                winning_price = cleaned_data.get("winning_price")
                if winning_price is not None and winning_price != winning_price.to_integral_value():
                    self.add_error("winning_price", "This auction only allows whole dollar amounts.")
        if not cleaned_data.get("auctiontos_winner") and cleaned_data.get("winning_price"):
            self.add_error("auctiontos_winner", "You need to set a winner")
        if cleaned_data.get("auctiontos_winner") and not cleaned_data.get("winning_price"):
            self.add_error("winning_price", "You need to set a sell price")
        return cleaned_data


class CreateEditAuctionTOS(forms.ModelForm):
    """Used for HTMX calls to update AuctionTOS.  For auction admins only."""

    def __init__(self, is_edit_form, auctiontos, auction, *args, **kwargs):
        self.is_edit_form = is_edit_form
        self.auction = auction
        self.auctiontos = auctiontos
        super().__init__(*args, **kwargs)
        problem_button_html = ""
        delete_button_html = ""
        if self.is_edit_form:
            problems_url = reverse(
                "auction_no_show",
                kwargs={
                    "slug": self.auction.slug,
                    "tos": self.auctiontos.bidder_number,
                },
            )
            problem_button_html = f"<a href={problems_url} class='btn text-dark bg-warning d-none d-md-inline'><i class='bi bi-exclamation-circle'></i> Problems</a>"
            post_url = f"/api/auctiontos/{self.auctiontos.pk}/"
            delete_url = reverse("auctiontosdelete", kwargs={"pk": self.auctiontos.pk})
            delete_button_html = f"<a href={delete_url} class='btn btn-danger d-none d-md-inline'><i class='bi bi-person-fill-x'></i> Delete</a>"
        else:
            post_url = f"/api/auctiontos/{self.auction.slug}/"
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "user-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div(
                    "bidder_number",
                    css_class="col-sm-4",
                ),
                Div(
                    "memo",
                    css_class="col-sm-8",
                ),
                css_class="row",
            ),
            "name",
            Div(
                Div(
                    "email",
                    css_class="col-sm-6",
                ),
                Div(
                    "phone_number",
                    css_class="col-sm-6",
                ),
                css_class="row",
            ),
            "address",
            "pickup_location",
            "selling_allowed",
            "bidding_allowed",
            Div(
                Div(
                    "is_club_member",
                    css_class="col-sm-6",
                ),
                Div(
                    "is_admin",
                    css_class="col-sm-6",
                ),
                css_class="row",
            ),
            Div(
                HTML(
                    f'{problem_button_html}{delete_button_html}<button type="button" class="btn btn-secondary" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(
                    f'<button hx-post="{post_url}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark">Save</button>'
                ),
                css_class="modal-footer",
            ),
        )
        self.fields["name"].required = True
        self.fields[
            "pickup_location"
        ].queryset = auction.location_qs  # PickupLocation.objects.filter(auction=self.auction).order_by('name')
        if self.is_edit_form:
            # hide fields if editing
            self.fields["bidder_number"].initial = self.auctiontos.bidder_number
            if self.auctiontos.unbanned_lot_count and self.auction.use_seller_dash_lot_numbering:
                self.fields[
                    "bidder_number"
                ].help_text = f"<span class=''>This user has already added {self.auctiontos.unbanned_lot_count} lots.</span> Changing this will not update lot numbers, but invoices will still be accurate"
            self.fields["memo"].initial = self.auctiontos.memo
            self.fields["name"].initial = self.auctiontos.name
            self.fields["email"].initial = self.auctiontos.email
            if self.auctiontos.pk and self.auctiontos.email_address_status == "BAD":
                self.fields[
                    "email"
                ].help_text = f"<span class='text-warning'>Emails sent to {self.auctiontos.email} have bounced</span>, try to get an updated email from this user."
            self.fields["phone_number"].initial = getattr(
                self.auctiontos, "phone_as_string", self.auctiontos.phone_number
            )
            self.fields["address"].initial = self.auctiontos.address
            self.fields["pickup_location"].initial = self.auctiontos.pickup_location.pk
            self.fields["is_admin"].initial = self.auctiontos.is_admin
            self.fields["is_club_member"].initial = self.auctiontos.is_club_member
            self.fields["selling_allowed"].initial = self.auctiontos.selling_allowed
            self.fields["bidding_allowed"].initial = self.auctiontos.bidding_allowed
            if auction.online_bidding == "disable" and not self.auctiontos:
                self.fields["bidding_allowed"].widget = HiddenInput()
            if auction.online_bidding == "disable" and self.auctiontos and self.auctiontos.bidding_allowed:
                self.fields["bidding_allowed"].widget = HiddenInput()
        else:
            self.fields["is_admin"].widget = HiddenInput()
            self.fields["bidding_allowed"].widget = HiddenInput()
            # special rule: default to the default location
            if auction.location_qs.count() == 1:
                self.fields["pickup_location"].initial = auction.location_qs.first()
        if auction.location_qs.count() == 1:
            self.fields["pickup_location"].widget = HiddenInput()
        if not auction.only_approved_sellers and not self.auctiontos:
            self.fields["selling_allowed"].widget = HiddenInput()
        if not auction.only_approved_sellers and self.auctiontos and self.auctiontos.selling_allowed:
            self.fields["selling_allowed"].widget = HiddenInput()
        help_text = "Check to take a cut of "
        if self.auction.lot_entry_fee_for_club_members:
            help_text += f"${self.auction.lot_entry_fee_for_club_members}"
        if self.auction.lot_entry_fee_for_club_members and self.auction.winning_bid_percent_to_club_for_club_members:
            help_text += " plus "
        if self.auction.winning_bid_percent_to_club_for_club_members:
            help_text += f"{self.auction.winning_bid_percent_to_club_for_club_members}%"
        if (
            not self.auction.lot_entry_fee_for_club_members
            and not self.auction.winning_bid_percent_to_club_for_club_members
        ):
            help_text = (
                "Check to charge <span class='text-warning'>no selling fees</span>.  Are your rules set up correctly?"
            )
        else:
            help_text += " instead of the standard fee for sold lots"
        self.fields["is_club_member"].help_text = help_text
        self.fields["is_club_member"].label = self.auction.alternative_split_label.capitalize()
        self.fields["memo"].help_text = None
        self.fields["bidder_number"].help_text = None
        self.fields["memo"].widget.attrs["placeholder"] = "Only visible to admins"
        self.fields["bidder_number"].widget.attrs["placeholder"] = "Auto generate"
        if self.auction.alternate_split_mode != "custom":
            # Off: the alternate split doesn't apply.  Club member discount: the flag is
            # managed automatically based on club membership.
            self.fields["is_club_member"].disabled = True
            self.fields["is_club_member"].widget = HiddenInput()
        if self.auction.is_club_managed:
            # In club-managed mode, these fields live on ClubMember and are managed via the club admin.
            for field_name in ("bidder_number", "bidding_allowed", "selling_allowed", "is_admin", "is_club_member"):
                self.fields[field_name].disabled = True
                self.fields[field_name].widget = HiddenInput()

    class Meta:
        model = AuctionTOS
        fields = [
            "bidder_number",
            "pickup_location",
            "is_admin",
            "name",
            "email",
            "phone_number",
            "address",
            "selling_allowed",
            "bidding_allowed",
            "is_club_member",
            "memo",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        if auction:
            if not auction.permission_check(self.user):
                self.add_error("auction", "How did you even manage to change this field?")
        bidder_number = cleaned_data.get("bidder_number")
        other_bidder_numbers = AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number)
        if self.auctiontos:
            other_bidder_numbers = other_bidder_numbers.exclude(pk=self.auctiontos.pk)
        if other_bidder_numbers.count():
            self.add_error("bidder_number", "This bidder number is already in this auction")
        email = cleaned_data.get("email")
        if email:
            other_emails = AuctionTOS.objects.filter(auction=self.auction, email=email)
            if self.auctiontos:
                other_emails = other_emails.exclude(pk=self.auctiontos.pk)
            if other_emails.count():
                self.add_error("email", "This email is already in this auction")
        return cleaned_data


class CreateBid(forms.ModelForm):
    # amount = forms.IntegerField()
    def __init__(self, *args, **kwargs):
        self.req = kwargs.pop("request", None)
        self.lot = kwargs.pop("lot", None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form-inline"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "user",
            "lot_number",
            "amount",
            Submit("submit", "Place bid", css_class="place-bid btn-info"),
        )
        self.fields["user"].widget = HiddenInput()
        self.fields["lot_number"].widget = HiddenInput()

    # def save(self, *args, **kwargs):
    #     kwargs['commit']=False
    #     obj = super().save(*args, **kwargs)
    #     logger.debug(self.req.user.id)
    #     #obj.user = self.req.user.id
    #     #logger.debug(str(obj.user)+ " has placed a bid on " + str(obj.lot_number))
    #     obj.save()
    #     return obj

    class Meta:
        model = Bid
        fields = [
            "user",
            "lot_number",
            "amount",
        ]


# class InvoiceUpdateForm(forms.ModelForm):
#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         self.helper = FormHelper()
#         self.helper.form_method = 'post'
#         self.helper.form_class = 'form'
#         self.helper.form_id = 'invoice-form'
#         self.helper.form_tag = True
#         self.helper.layout = Layout(
#             'memo',
#             HTML("<h5>Adjust</h5>"),
#             Div(
#             Div('adjustment_direction',css_class='col-lg-3',),
#             PrependedAppendedText('adjustment', '$', '.00',wrapper_class='col-lg-3', ),
#             Div('adjustment_notes',css_class='col-lg-6',),
#             css_class='row',
#             ),
#             Submit('submit', 'Save', css_class='btn-success'),
#         )
#         self.fields['adjustment_direction'].label = ""
#         self.fields['adjustment'].label = ""
#         self.fields['adjustment_notes'].label = ""
#         self.fields['adjustment_notes'].help_text = f"Adjustment reason will be visible to the user"

#     class Meta:
#         model = Invoice
#         fields = [
#             'adjustment_direction',
#             'adjustment',
#             'adjustment_notes',
#             'memo',
#         ]


class AuctionNoShowForm(forms.Form):
    """ban, refund lots.  Confirmation dialog for auction admins only"""

    refund_sold_lots = forms.BooleanField(required=False)
    refund_bought_lots = forms.BooleanField(required=False)
    leave_negative_feedback = forms.BooleanField(required=False)
    ban_this_user = forms.BooleanField(required=False)

    def __init__(self, auction, tos, *args, **kwargs):
        self.auction = auction
        self.tos = tos
        submit_button_html = f'<button hx-post="{reverse("auction_no_show_dialog", kwargs={"slug": self.auction.slug, "tos": self.tos.bidder_number})}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark">Take actions</button>'
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "ban-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div(
                    "refund_sold_lots",
                    css_class="col-sm-12",
                ),
                Div(
                    "refund_bought_lots",
                    css_class="col-sm-12",
                ),
                Div(
                    "leave_negative_feedback",
                    css_class="col-sm-12",
                ),
                Div(
                    "ban_this_user",
                    css_class="col-sm-12",
                ),
                css_class="row",
            ),
            Div(
                HTML(
                    '<button type="button" class="btn btn-secondary me-auto" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(submit_button_html),
                css_class="modal-footer",
            ),
        )
        sold_lot_count = self.tos.lots_qs.filter(winning_price__isnull=False).count()
        unsold_lot_count = self.tos.lots_qs.filter(winning_price__isnull=True).count()
        bought_lots_count = self.tos.bought_lots_qs.count()
        self.fields[
            "refund_sold_lots"
        ].help_text = f"Issue a 100% refund to the buyers for {sold_lot_count} sold lot(s), and remove {unsold_lot_count} unsold lot(s).  You will need to send money to any users whose invoice is not open."
        self.fields[
            "refund_bought_lots"
        ].help_text = f"Issue a 100% refund for to the sellers for {bought_lots_count} lots this user won.  You will need to send money to any users whose invoice is not open."
        self.fields[
            "leave_negative_feedback"
        ].help_text = "Leave negative feedback about this user on all lots this seller sold or won, to warn other people about them in the future."
        self.fields["ban_this_user"].help_text = "Block this user from joining any of your future auctions."

    class Meta:
        fields = [
            "refund_sold_lots",
            "refund_sold_lots",
            "leave_negative_feedback",
            "ban_this_user",
        ]


class BulkSellLotsToOnlineHighBidder(forms.Form):
    """confirmation dialog for auction admins only"""

    got_it = forms.BooleanField(required=True, label="Yes, I understand this cannot be undone")

    def __init__(self, auction, query, queryset, *args, **kwargs):
        self.auction = auction
        self.queryset = queryset
        # submit_button_html = f'<button hx-vals="{query":"{query}"} hx-post="{reverse("bulk_set_lots_won", kwargs={"slug": self.auction.slug})}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark">Mark {self.queryset.count()} lots sold</button>'
        submit_button_html = f'<button hx-vals=\'{{"query": "{query}"}}\' hx-post="{reverse("bulk_set_lots_won", kwargs={"slug": self.auction.slug})}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark">Mark {self.queryset.count()} lots sold</button>'
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "lots-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div(
                    "got_it",
                    css_class="col-sm-12",
                ),
                css_class="row",
            ),
            Div(
                HTML(
                    '<button type="button" class="btn btn-secondary me-auto" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(submit_button_html),
                css_class="modal-footer",
            ),
        )

    class Meta:
        fields = [
            "got_it",
        ]


class ChangeInvoiceStatusForm(forms.Form):
    """confirmation dialog for auction admins only"""

    send_invoice_ready_notification_emails = forms.BooleanField(required=False)

    def __init__(self, auction, invoice_count, show_checkbox, post_target_url, *args, **kwargs):
        self.auction = auction
        self.invoice_count = invoice_count
        submit_button_html = ""
        self.show_checkbox = show_checkbox
        if self.invoice_count:
            submit_button_html = f'<button hx-post="{reverse(post_target_url, kwargs={"slug": self.auction.slug})}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark">Change invoices</button>'
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "invoices-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div(
                    "send_invoice_ready_notification_emails",
                    css_class="col-sm-12",
                ),
                css_class="row",
            ),
            Div(
                HTML(
                    '<button type="button" class="btn btn-secondary me-auto" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(submit_button_html),
                css_class="modal-footer",
            ),
        )
        self.fields["send_invoice_ready_notification_emails"].initial = self.auction.email_users_when_invoices_ready
        self.fields[
            "send_invoice_ready_notification_emails"
        ].help_text = "Users get a link to view their invoice.  You probably want to check this."
        if not self.show_checkbox:
            self.fields["send_invoice_ready_notification_emails"].widget = HiddenInput()

    class Meta:
        fields = [
            "send_invoice_ready_notification_emails",
        ]


class EnableBiddingForAllForm(forms.Form):
    """Confirmation dialog for the bulk 'enable bidding' action on the auction users page."""

    def __init__(self, auction, user_count, *args, **kwargs):
        self.auction = auction
        self.user_count = user_count
        super().__init__(*args, **kwargs)
        submit_button_html = ""
        if user_count:
            post_url = reverse("auction_enable_bidding_for_all", kwargs={"slug": auction.slug})
            submit_button_html = (
                f'<button hx-post="{post_url}" hx-target="#modals-here" type="submit" '
                'class="btn btn-success text-dark">Enable bidding</button>'
            )
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "enable-bidding-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                HTML(
                    '<button type="button" class="btn btn-secondary me-auto" '
                    'onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(submit_button_html),
                css_class="modal-footer",
            ),
        )


class LotRefundForm(forms.ModelForm):
    """Show the status of existing invoices and allow partial or full refunds"""

    class Meta:
        model = Lot
        fields = [
            "partial_refund_percent",
            "banned",
        ]

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data

    def __init__(self, *args, **kwargs):
        self.lot = kwargs.pop("lot")
        super().__init__(*args, **kwargs)
        if not self.lot.sold:
            self.fields["partial_refund_percent"].widget = HiddenInput()
        else:
            self.fields["banned"].widget = HiddenInput()

        # Add Square refund info message if applicable
        square_refund_msg = ""
        if self.lot.square_refund_possible and not self.lot.no_more_refunds_possible:
            square_refund_msg = '<div class="alert alert-info mt-3"><i class="bi bi-square"></i> <strong>Square refund will be automatically issued</strong> when you save this form.</div>'

        save_button_html = f'<button hx-post="{reverse("lot_refund", kwargs={"pk": self.lot.pk})}" hx-target="#modals-here" type="submit" class="btn btn-success text-dark ms-2">Save</button>'
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form"
        self.helper.form_id = "invoices-form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                PrependedAppendedText(
                    "partial_refund_percent",
                    "",
                    "%",
                    wrapper_class="col-lg-3",
                ),
                css_class="col-md-12",
            ),
            HTML(square_refund_msg) if square_refund_msg else Div(),
            Div(
                Div(
                    "banned",
                    css_class="col-md-12",
                ),
            ),
            Div(
                HTML(
                    '<button type="button" class="btn btn-secondary me-auto" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(save_button_html),
                css_class="modal-footer",
            ),
        )
        self.fields["partial_refund_percent"].initial = self.lot.partial_refund_percent
        self.fields["banned"].initial = self.lot.banned
        self.fields["partial_refund_percent"].label = "Refund percent"
        self.fields["banned"].label = "Remove this lot from the auction"
        self.fields[
            "banned"
        ].help_text = (
            "Users will no longer be able to see this lot, it will be set to unsold, and the seller will not be paid."
        )


class AuctionJoin(forms.ModelForm):
    i_agree = forms.BooleanField(required=True)

    def __init__(self, user, auction, next_url=None, *args, **kwargs):
        self.user = user
        self.auction = auction
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_class = "form-inline"
        self.helper.form_id = "rule-form"
        self.helper.form_tag = True
        self.helper.form_action = reverse("auction_main", kwargs={"slug": auction.slug})
        if next_url:
            # Carry ?next= through the POST so get_success_url() (which reads request.GET["next"])
            # can return the user to where they came from (e.g. the lot page they joined from).
            from urllib.parse import quote

            self.helper.form_action = f"{self.helper.form_action}?next={quote(next_url, safe='')}"
        self.helper.layout = Layout(
            "i_agree",
            "time_spent_reading_rules",
            "pickup_location",
            Submit("submit", "Join auction", css_class="agree_tos btn-success text-dark"),
        )
        self.fields[
            "pickup_location"
        ].queryset = auction.location_qs  # PickupLocation.objects.filter(auction=self.auction).order_by('name')
        self.fields["time_spent_reading_rules"].widget = HiddenInput()
        if self.auction.multi_location:
            self.fields["i_agree"].initial = True
            self.fields["i_agree"].widget = HiddenInput()
            self.fields["pickup_location"].label = "Yes, I will be at "
        else:
            # single location auction
            self.fields["pickup_location"].widget = HiddenInput()
            if self.auction.all_location_count == 1:  # note: number_of_locations only gives you non-default locations
                location = auction.location_qs[0]
                self.fields["pickup_location"].initial = location
                if location.pickup_by_mail:
                    self.fields["i_agree"].label = "Yes, mail me my lots"
                else:
                    self.fields["i_agree"].label = "Yes, I will be at this auction"

    class Meta:
        model = AuctionTOS
        fields = [
            "i_agree",
            "pickup_location",
            "time_spent_reading_rules",
        ]


class PickupLocationForm(forms.ModelForm):
    MAIL_CHOICES = [
        (False, "This is an in-person pickup location"),
        (True, "Lots will be mailed (or emailed) to the winner"),
    ]
    mail_or_not = forms.ChoiceField(
        choices=MAIL_CHOICES,
        widget=forms.RadioSelect,
        label="",
        help_text="It's up to you to calculate any shipping charges after the auction ends",
        required=False,
    )

    class Meta:
        model = PickupLocation
        exclude = [
            "user",
            "latitude",
            "longitude",
            "is_default",
            "pickup_location_contact_name",
            "pickup_location_contact_phone",
            "pickup_location_contact_email",
        ]
        widgets = {
            "pickup_time": DateTimePickerInput(),
            "second_pickup_time": DateTimePickerInput(),
            "description": forms.Textarea,
            "auction": forms.HiddenInput,
        }

    def __init__(self, user, auction, *args, **kwargs):
        timezone.activate(kwargs.pop("user_timezone"))
        self.is_edit_form = kwargs.pop("is_edit_form")
        self.pickup_location = kwargs.pop("pickup_location")
        super().__init__(*args, **kwargs)
        self.user = user
        self.auction = auction
        self.fields["description"].widget.attrs = {"rows": 3}
        show_name_contact_fields = True
        if self.auction.all_location_count < 2:
            show_name_contact_fields = False
        if self.auction.all_location_count > 0 and not self.is_edit_form:
            show_name_contact_fields = True
        if not show_name_contact_fields:
            self.fields["name"].widget = forms.HiddenInput()
            self.fields["contact_person"].widget = forms.HiddenInput()
        if not self.auction.multi_location:
            # to keep things simple when creating a new auction with only one location
            self.fields["second_pickup_time"].widget = forms.HiddenInput()
            # self.fields['description'].widget=forms.HiddenInput()
            # these have been removed in favor of 'contact_person'
            # self.fields['pickup_location_contact_name'].widget=forms.HiddenInput()
            # self.fields['pickup_location_contact_phone'].widget=forms.HiddenInput()
            # self.fields['pickup_location_contact_email'].widget=forms.HiddenInput()
            # self.fields['users_must_coordinate_pickup'].widget=forms.HiddenInput()
        self.fields["mail_or_not"].initial = "False"
        if self.instance.pk:
            if self.instance.pickup_by_mail:
                self.fields["mail_or_not"].initial = "True"
        if not self.auction.is_online:
            # hide several fields for in-person auctions
            self.fields["mail_or_not"].widget = forms.HiddenInput()
            self.fields["users_must_coordinate_pickup"].widget = forms.HiddenInput()
            self.fields["pickup_time"].widget = forms.HiddenInput()
            self.fields[
                "description"
            ].help_text = "Directions or notes about this location.  This text will be shown to users."

        # if self.user.is_superuser:
        #     self.fields['auction'].queryset = Auction.objects.filter(date_end__gte=timezone.now()).order_by('date_end')
        # else:
        #     self.fields['auction'].queryset = Auction.objects.filter(created_by=self.user).filter(date_end__gte=timezone.now()).order_by('date_end')
        self.fields["contact_person"].queryset = self.auction.auction_admins_qs
        self.fields["contact_person"].label_from_instance = lambda obj: f"{obj.name}"
        delete_button_html = ""
        if self.is_edit_form:
            delete_button_html = f"<a href='{reverse('delete_pickup', kwargs={'pk': self.pickup_location.pk})}' class='btn btn-danger ms-2'>Delete this location</a>"
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "location-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.fields["auction"].initial = auction
        self.helper.layout = Layout(
            "mail_or_not",
            Div(
                Div(
                    Div(
                        "name",
                        css_class="col-md-6",
                    ),
                    Div(
                        "contact_person",
                        css_class="col-md-6",
                    ),
                    css_class="row",
                ),
                "auction",
                # HTML("<h4>Contact info</h4>"),
                # Div(
                #     Div('pickup_location_contact_name',css_class='col-md-6',),
                #     Div('pickup_location_contact_phone',css_class='col-md-6',),
                #     Div('pickup_location_contact_email',css_class='col-md-6',),
                #     css_class='row',
                # ),
                Div(
                    Div(
                        "users_must_coordinate_pickup",
                        css_class="col-md-4",
                    ),
                    Div(
                        "pickup_time",
                        css_class="col-md-4",
                    ),
                    Div(
                        "second_pickup_time",
                        css_class="col-md-4",
                    ),
                    css_class="row",
                ),
                "address",
                "location_coordinates",
                Div(
                    HTML(
                        "The pin on the map must be at the <span class='text-warning'>exact location of the pickup location!</span><br><small>People will get directions based on this pin, and will get lost if it's not in the right place</small>"
                    ),
                ),
                # 'allow_selling_by_default',
                # 'allow_bidding_by_default',
                css_id="non-mail",
            ),
            "description",
            HTML(f"{delete_button_html}"),
            Submit("submit", "Save", css_class="bg-success ms-2"),
        )

    def clean(self):
        cleaned_data = super().clean()
        auction = cleaned_data.get("auction")
        if auction:
            if not auction.permission_check(self.user):
                self.add_error("auction", "You can only add pickup locations to your own auctions")
        if cleaned_data.get("mail_or_not") == "False":
            if not cleaned_data.get("location_coordinates"):
                self.add_error("address", "Search here to set the location on the map below")
        else:
            existing_mail_locations = (
                PickupLocation.objects.exclude(pk=self.instance.pk).filter(auction=auction, pickup_by_mail=True).count()
            )
            if existing_mail_locations:
                self.add_error("mail_or_not", "You can't have more than one mail location")
        return cleaned_data


class CreateImageForm(forms.ModelForm):
    class Meta:
        model = LotImage
        fields = [
            "image",
            "url",
            "image_source",
            "caption",
        ]
        exclude = [
            "is_primary",
            "lot_number",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields[
            "image"
        ].help_text = "Select an image to upload, or paste one from your clipboard (Ctrl+V) anywhere on this page"
        # Marking the input as image-only lets the native app's WebView file chooser offer the camera
        # (many WebViews only surface "Take photo" when accept is set to an image type). We deliberately
        # do NOT set `capture`, so picking from the photo library stays available too.
        self.fields["image"].widget.attrs["accept"] = "image/*"
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "auction-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "image",
            HTML(
                '<div id="pasted-image-preview" class="d-none mb-3">'
                '<img src="" alt="Preview of selected image" class="img-thumbnail" style="max-height: 200px;">'
                '<div class="text-muted small">Click Save to upload this image</div>'
                "</div>"
            ),
            "url",
            Div(
                Div(
                    "image_source",
                    css_class="col-md-4",
                ),
                Div(
                    "caption",
                    css_class="col-md-8",
                ),
                css_class="row",
            ),
            Submit("submit", "Save", css_class="create-update-image btn-success"),
        )

    def clean_url(self):
        """Validate that the URL points to an image"""
        url = self.cleaned_data.get("url")
        if not url:
            return url
        return validate_image_url(url)

    def clean_image(self):
        """Reject corrupt/unsupported uploads with a friendly message before they hit save.

        Django's ImageField only runs Pillow's ``verify()`` (a header check), which passes
        truncated or otherwise broken files that then explode during thumbnail generation.
        Here we fully decode a freshly uploaded file so image problems become a nice inline
        field error rather than a server error blamed on the user's photo.
        """
        image = self.cleaned_data.get("image")
        # Only validate a newly uploaded file; leave an unchanged, already-stored image
        # (on the edit view) alone.
        if isinstance(image, UploadedFile):
            validate_uploaded_image(image)
        return image

    def clean(self):
        cleaned_data = super().clean()
        image = cleaned_data.get("image")
        url = cleaned_data.get("url")
        if not image and not url:
            msg = "Please either upload an image or provide a URL."
            self.add_error("image", msg)
            self.add_error("url", msg)
        return cleaned_data


class CreateAuctionForm(forms.ModelForm):
    """Simplified form with just date, online, and name fields"""

    is_online = forms.BooleanField(required=False, widget=forms.HiddenInput())
    cloned_from = forms.CharField(required=False, widget=forms.HiddenInput())

    class Meta:
        model = Auction
        fields = [
            "date_start",
            "title",
        ]
        widgets = {
            "date_start": DateTimePickerInput(),
            # 'is_online': HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        self.auction = None  # this will be the instance of Auction to clone from
        self.cloned_from = kwargs.pop("cloned_from")  # slug only at this point
        timezone.activate(kwargs.pop("user_timezone"))
        super().__init__(*args, **kwargs)

        last_auction = "the last auction I created"
        last_auction_tooltip = (
            "You haven't created any auctions on this site yet. Once you have, you can easily reuse rules!"
        )
        last_auction_state = "disabled"  # class of the copy my last auction button
        self.auction = None
        if self.cloned_from:
            # did this user ACTUALLY create this auction, or are they stealing rules from someone else?
            self.auction = Auction.objects.exclude(is_deleted=True).filter(slug=self.cloned_from).first()
            if self.auction:
                if not self.auction.permission_check(self.user):
                    self.auction = None
        if not self.auction:
            # either ?copy was not set, or the user didn't make that auction - doesn't matter
            self.auction = auction_to_copy(self.user)
        if self.auction:
            self.fields["cloned_from"].initial = str(self.auction.slug)
            last_auction = str(self.auction)
            last_auction_tooltip = "Same rules and locations, but with new dates and users."
            last_auction_state = ""
            self._seed_picker_time_from(self.auction)

        if self.instance.pk:
            # editing existing auction
            logger.debug("wait, no, we should never get here!!!")
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "auction-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "is_online",
            "cloned_from",
            "date_start",
            "title",
            HTML("<div id='auction_type_fields'><h5>What kind of auction is this?</h5>"),
            Submit(
                "online",
                "Create online auction",
                css_id="auction-online",
                css_class="submit-button create-auction bg-success text-dark",
            ),
            Div(
                HTML(
                    "<span class='text-muted'><ul><li>An auction where bidding ends automatically at a specified time.</li><li>Users will create an account on this site to join your auction.</li><li>Lots will be brought to one or more locations for exchange after bidding ends.</li></span>"
                ),
            ),
            Submit(
                "offline",
                value="Create in-person auction",
                css_id="auction-offline",
                css_class="submit-button bg-success text-dark",
            ),
            Div(
                HTML(
                    "<span class='text-muted'><ul><li>You or your auctioneer will manually set the winners of lots.</li><li>All lots will be brought to your auction's location before bidding starts.</li></ul></span></div>"
                ),
            ),
            Submit(
                "clone",
                "Copy " + last_auction,
                css_id="auction-copy",
                css_class="submit-button btn-info " + last_auction_state,
            ),
            Div(
                HTML("<span class='text-muted'><ul><li>" + last_auction_tooltip + "</li></ul></span>"),
            ),
        )

    def _seed_picker_time_from(self, auction):
        """Open the date picker on today at *auction*'s start time, instead of at the time of day
        the form happens to be open.

        A club's auction is at the same hour every time it runs, so "7:00 PM" is nearly always the
        right answer and "2:14 PM, because that is when I clicked" never is. Two picker options do
        it: ``viewDate`` is the moment the calendar opens on, and ``selectDay`` keeps the viewDate's
        *time* and only replaces its day — so whichever day is clicked comes back at the old
        auction's time. ``useCurrent: false`` is what stops the picker stamping the current time
        into the empty field the moment it opens, which is the behaviour being replaced; the field
        stays blank until a day is actually picked, so nobody creates an auction dated today by
        accident.
        """
        if not auction.date_start:
            return
        start = timezone.localtime(auction.date_start)
        seed = timezone.localtime(timezone.now()).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
        self.fields["date_start"].widget = DateTimePickerInput(
            options={"useCurrent": False, "viewDate": seed.strftime("%Y-%m-%dT%H:%M:%S")}
        )


class AuctionEditForm(forms.ModelForm):
    """Make changes to an auction"""

    user_cut = forms.IntegerField(required=False, help_text="This plus the club cut must be 100%")
    club_member_cut = forms.IntegerField(
        required=False,
        help_text="This plus the alternate club cut must be 100%",
        label="Alternate user cut",
    )
    club = forms.ModelChoiceField(
        queryset=Club.objects.none(),
        required=False,
        empty_label="None",
        help_text="Associate this auction with a club.",
    )

    class Meta:
        model = Auction
        fields = [
            "summernote_description",
            "lot_entry_fee",
            "registration_fee",
            "unsold_lot_fee",
            "winning_bid_percent_to_club",
            "date_start",
            "date_end",
            "lot_submission_start_date",
            "lot_submission_end_date",
            "promote_this_auction",
            "max_lots_per_user",
            "allow_additional_lots_as_donation",
            "email_users_when_invoices_ready",
            "add_membership_fee_to_invoices_for_expired_members",
            "pre_register_lot_discount_percent",
            "only_approved_sellers",
            "only_approved_bidders",
            "invoice_payment_instructions",
            "invoice_rounding",
            "only_whole_dollar_bids",
            "minimum_bid",
            "alternate_split_mode",
            "winning_bid_percent_to_club_for_club_members",
            "lot_entry_fee_for_club_members",
            "registration_fee_for_club_members",
            "alternative_split_label",
            "club_member_discount",
            "force_donation_threshold",
            "require_phone_number",
            "tax",
            "online_bidding",
            "date_online_bidding_ends",
            "date_online_bidding_starts",
            "allow_deleting_bids",
            "auto_add_images",
            "message_users_when_lots_sell",
            "copy_users_when_copying_this_auction",
            "use_seller_dash_lot_numbering",
            "enable_online_payments",
            "enable_square_payments",
            "club",
            "manage_users_through_club",
            "allow_self_checkin",
        ]
        widgets = {
            "date_start": DateTimePickerInput(),
            "date_end": DateTimePickerInput(),
            "lot_submission_start_date": DateTimePickerInput(),
            "lot_submission_end_date": DateTimePickerInput(),
            "date_online_bidding_ends": DateTimePickerInput(),
            "date_online_bidding_starts": DateTimePickerInput(),
            "summernote_description": SummernoteWidget(),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        self.cloned_from = kwargs.pop("cloned_from")
        timezone.activate(kwargs.pop("user_timezone"))
        super().__init__(*args, **kwargs)
        # self.fields["summernote_description"].widget.attrs = {"rows": 10}
        self.fields["winning_bid_percent_to_club"].label = "Club cut"
        self.fields["winning_bid_percent_to_club_for_club_members"].label = "Alternate club cut"
        self.fields["date_start"].label = "Bidding opens"
        self.fields["date_end"].label = "Bidding ends"
        self.fields["email_users_when_invoices_ready"].label = "Invoice notifications"
        self.fields[
            "email_users_when_invoices_ready"
        ].help_text = "Send an email to users when their invoice is ready or paid"
        self.fields["alternative_split_label"].widget.attrs = {"placeholder": "Club Member"}
        # Hidden via js unless a club is selected; don't block submission when it's not shown.
        self.fields["club_member_discount"].required = False
        # Optional fees: leaving them blank means "no fee", not a validation error. The model default
        # is already 0 and the column is NOT NULL, so clean() coerces a blank submission back to 0.
        self.fields["registration_fee"].required = False
        self.fields["registration_fee_for_club_members"].required = False
        self.fields["invoice_payment_instructions"].widget.attrs = {"placeholder": "Send money to paypal.me/yourpaypal"}

        # Build club queryset: clubs where the user has admin/edit/manage_auctions permission
        if self.user.is_superuser:
            permitted_club_ids = list(Club.objects.values_list("pk", flat=True))
        else:
            permitted_club_ids = list(
                ClubMember.objects.filter(
                    user=self.user,
                    is_deleted=False,
                )
                .filter(Q(permission_admin=True) | Q(permission_edit_club=True) | Q(permission_manage_auctions=True))
                .values_list("club_id", flat=True)
            )
        # Always include the currently saved club so admins without club membership can still edit
        club_id_set = set(permitted_club_ids)
        if self.instance and self.instance.pk and self.instance.club_id:
            club_id_set.add(self.instance.club_id)
        self.fields["club"].queryset = Club.objects.filter(pk__in=club_id_set).order_by("name")
        self.fields["club"].initial = self.instance.club if (self.instance and self.instance.pk) else None
        single_club = get_single_club(create=False)
        self.single_club_mode = bool(getattr(settings, "SINGLE_CLUB_MODE", False) and single_club)
        if self.single_club_mode:
            # Only one club exists on the whole site: hide the club picker and pin
            # this auction to it. Participants are always managed through that club,
            # so drop the "Off" option but still let admins choose how (all/check-in).
            self.fields["club"].queryset = Club.objects.filter(pk=single_club.pk)
            self.fields["club"].initial = single_club
            self.fields["club"].widget = forms.HiddenInput()
            self.fields["copy_users_when_copying_this_auction"].widget = forms.HiddenInput()
            self.fields["manage_users_through_club"].choices = [
                choice for choice in Auction.MANAGE_USERS_CHOICES if choice[0]
            ]
            self.fields["manage_users_through_club"].label = "Manage participants through the club"
            if not (self.instance.pk and self.instance.manage_users_through_club):
                self.fields["manage_users_through_club"].initial = SINGLE_CLUB_DEFAULT_MANAGE_MODE

        # Resolve which payment accounts apply to this auction:
        # - club auctions: the club's linked sellers (or site PayPal if enabled).
        # - non-club auctions: the auction creator's personal sellers.
        club = self.instance.club if (self.instance and self.instance.pk) else None
        effective_creator = self.instance.created_by if (self.instance and self.instance.created_by) else self.user

        if club:
            paypal_seller = club.effective_paypal_seller
            square_seller = club.effective_square_seller
            uses_site_paypal = club.uses_site_paypal
        else:
            paypal_seller = PayPalSeller.objects.filter(user=effective_creator).first()
            square_seller = SquareSeller.objects.filter(user=effective_creator).first()
            uses_site_paypal = bool(
                effective_creator
                and effective_creator.is_superuser
                and settings.PAYPAL_CLIENT_ID
                and settings.PAYPAL_SECRET
            )

        if club:
            # When auction is tied to a club, payments are controlled by club settings
            self.fields["enable_online_payments"].widget = forms.HiddenInput()
            self.fields["enable_square_payments"].widget = forms.HiddenInput()
        else:
            if paypal_seller:
                self.fields["enable_online_payments"].help_text += f"<br>Payments sent to {paypal_seller}"
            elif uses_site_paypal:
                self.fields["enable_online_payments"].help_text += "<br>Payments go to the site's PayPal account"
            else:
                # Hide the field if no PayPal route is configured.
                self.fields["enable_online_payments"].widget = forms.HiddenInput()

            if square_seller:
                self.fields["enable_square_payments"].help_text += f"<br>Payments sent to {square_seller}"
            else:
                # Square requires an actual linked seller record (no site fallback).
                self.fields["enable_square_payments"].widget = forms.HiddenInput()

        # These fields are shown/hidden via JavaScript based on the club selection.
        # We always render real widgets so the JS can toggle them; server validation
        # already rejects the combination of no-club + enabled flag.
        if self.instance.pk and self.instance.manage_users_through_club:
            # When club-managed, copy_users is irrelevant — the new auction gets members from the club
            self.fields["copy_users_when_copying_this_auction"].widget = forms.HiddenInput()
            has_activity = (
                Lot.objects.filter(auction=self.instance, is_deleted=False).exists()
                or Invoice.objects.filter(auction=self.instance).exists()
            )
            if has_activity:
                # Lock club-managed mode once lots or invoices exist to prevent disabling it
                self.fields["manage_users_through_club"].disabled = True
                self.fields["manage_users_through_club"].help_text = "Cannot be changed once lots or invoices exist."
                self.fields["club"].disabled = True
                self.fields[
                    "club"
                ].help_text = "Cannot be changed while lots or invoices exist in a club-managed auction."
            else:
                # No activity yet — allow toggling off or changing the club
                self.fields[
                    "manage_users_through_club"
                ].help_text = "Changing this will delete all existing participant records for this auction."
        else:
            # Membership fee only applies when club-managed mode is enabled
            self.fields["add_membership_fee_to_invoices_for_expired_members"].widget = forms.HiddenInput()
        # clean_manage_users_through_club rejects enabling on non-empty auctions
        # self.fields['notes'].help_text = "Foo"
        if self.instance.is_online and not self.single_club_mode:
            # Check-in mode only applies to in-person events, so don't offer it for online auctions.
            # Single-club mode is the exception: it always manages participants through the club and
            # defaults to check-in, so we keep that option even for online single-club auctions.
            self.fields["manage_users_through_club"].choices = [
                choice for choice in self.fields["manage_users_through_club"].choices if choice[0] != "checkin"
            ]
            self.fields[
                "lot_submission_end_date"
            ].help_text = "This should be 1-24 hours before the end of your auction"
            self.fields["online_bidding"].widget = forms.HiddenInput()
            self.fields["message_users_when_lots_sell"].widget = forms.HiddenInput()
            self.fields["pre_register_lot_discount_percent"].widget = forms.HiddenInput()
            # self.fields['set_lot_winners_url'].widget=forms.HiddenInput()
            self.fields["date_online_bidding_starts"].widget = forms.HiddenInput()
            self.fields["date_online_bidding_ends"].widget = forms.HiddenInput()
        else:
            # self.fields["only_approved_bidders"].widget = forms.HiddenInput()
            self.fields["unsold_lot_fee"].widget = forms.HiddenInput()
            self.fields["online_bidding"].help_text = "Most auctions should leave this off, it confuses people"
            self.fields[
                "date_end"
            ].help_text = "You should probably leave this blank so that you can manually set winners. This field has been indefinitely set to hidden - see https://github.com/iragm/fishauctions/issues/116"
            self.fields["date_end"].widget = forms.HiddenInput()
            self.fields[
                "lot_submission_end_date"
            ].help_text = "This should probably be before bidding starts.  Admins (you) can add more lots at any time, this only restricts users."
            self.fields[
                "email_users_when_invoices_ready"
            ].help_text = "Email users a link to view their invoice.  Only works if you enter the user's email address when adding them to your auction"
        self.fields["date_start"].help_text = "When the auction actually starts"
        self.fields["user_cut"].initial = 100 - self.instance.winning_bid_percent_to_club
        self.fields["club_member_cut"].initial = 100 - self.instance.winning_bid_percent_to_club_for_club_members

        # Get currency symbol from the auction creator (when editing) or current user (when creating)
        if self.instance and self.instance.pk and self.instance.created_by:
            # Editing an existing auction - use the auction creator's currency
            currency = self.instance.created_by.userdata.currency
        elif self.user and hasattr(self.user, "userdata"):
            # Creating a new auction - use the current user's currency
            currency = self.user.userdata.currency
        else:
            # Fallback to USD
            currency = "USD"
        currency_symbol = get_currency_symbol(currency)

        def slot(field_name, visible_element):
            """Place a field in its grid column when visible, but render just the
            bare hidden input (no empty column) when its widget has been switched
            to HiddenInput above. This keeps the value in the POST without leaving
            a blank cell in the row. See auction_edit_form.html for the JS-toggled
            fields, which collapse their own column via toggleCol()."""
            if isinstance(self.fields[field_name].widget, forms.HiddenInput):
                return field_name
            return visible_element

        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "auction-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            "summernote_description",
            HTML("<h4>Dates</h4>"),
            Div(
                Div(
                    "lot_submission_start_date",
                    css_class="col-md-3",
                ),
                Div(
                    "lot_submission_end_date",
                    css_class="col-md-3",
                ),
                Div(
                    "date_start",
                    css_class="col-md-3",
                    label="Bidding opens",
                ),
                slot(
                    "date_end",
                    Div(
                        "date_end",
                        css_class="col-md-3",
                    ),
                ),
                css_class="row",
            ),
            HTML("<h4>Lot fees</h4>"),
            Div(
                slot(
                    "unsold_lot_fee",
                    PrependedAppendedText(
                        "unsold_lot_fee",
                        currency_symbol,
                        ".00",
                        wrapper_class="col-lg-3",
                    ),
                ),
                PrependedAppendedText(
                    "lot_entry_fee",
                    currency_symbol,
                    ".00",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "registration_fee",
                    currency_symbol,
                    ".00",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "winning_bid_percent_to_club",
                    "",
                    "%",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "user_cut",
                    "",
                    "%",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "force_donation_threshold",
                    currency_symbol,
                    ".00",
                    wrapper_class="col-lg-3",
                ),
                css_class="row",
            ),
            HTML("<h4>Lot fee discounts</h4>"),
            Div(
                Div(
                    "alternate_split_mode",
                    css_class="col-lg-3",
                ),
                Div(
                    "alternative_split_label",
                    css_class="col-lg-9",
                ),
                css_class="row",
            ),
            Div(
                slot(
                    "pre_register_lot_discount_percent",
                    PrependedAppendedText(
                        "pre_register_lot_discount_percent",
                        "",
                        "%",
                        wrapper_class="col-lg-3",
                    ),
                ),
                PrependedAppendedText(
                    "lot_entry_fee_for_club_members",
                    currency_symbol,
                    ".00",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "registration_fee_for_club_members",
                    currency_symbol,
                    ".00",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "winning_bid_percent_to_club_for_club_members",
                    "",
                    "%",
                    wrapper_class="col-lg-3",
                ),
                PrependedAppendedText(
                    "club_member_cut",
                    "",
                    "%",
                    wrapper_class="col-lg-3",
                ),
                css_class="row",
            ),
            HTML("<h4>Lot permissions</h4>"),
            Div(
                slot(
                    "online_bidding",
                    Div(
                        "online_bidding",
                        css_class="col-md-3",
                    ),
                ),
                slot(
                    "date_online_bidding_starts",
                    Div(
                        "date_online_bidding_starts",
                        css_class="col-md-3",
                    ),
                ),
                slot(
                    "date_online_bidding_ends",
                    Div(
                        "date_online_bidding_ends",
                        css_class="col-md-3",
                    ),
                ),
                Div(
                    "allow_deleting_bids",
                    css_class="col-md-3",
                ),
                css_class="row",
            ),
            Div(
                Div(
                    "max_lots_per_user",
                    css_class="col-md-4",
                ),
                Div(
                    "allow_additional_lots_as_donation",
                    css_class="col-md-4",
                ),
                Div(
                    "only_approved_sellers",
                    css_class="col-md-4",
                ),
                Div(
                    "only_approved_bidders",
                    css_class="col-md-4",
                ),
                slot(
                    "copy_users_when_copying_this_auction",
                    Div(
                        "copy_users_when_copying_this_auction",
                        css_class="col-md-4",
                    ),
                ),
                Div(
                    "use_seller_dash_lot_numbering",
                    css_class="col-md-4",
                ),
                css_class="row",
            ),
            HTML("<h4>Club</h4>"),
            Div(
                slot(
                    "club",
                    Div(
                        "club",
                        css_class="col-md-6",
                    ),
                ),
                Div(
                    "manage_users_through_club",
                    css_class="col-md-6",
                ),
                # Only applies to check-in mode; shown/hidden by update_club_fields() in
                # auction_edit_form.html as the mode select changes.
                Div(
                    "allow_self_checkin",
                    css_class="col-md-6",
                ),
                PrependedAppendedText(
                    "club_member_discount",
                    currency_symbol,
                    ".00",
                    wrapper_class="col-md-6",
                ),
                css_class="row",
            ),
            HTML("<h4>General</h4>"),
            Div(
                Div(
                    "require_phone_number",
                    css_class="col-md-3",
                ),
                Div(
                    "email_users_when_invoices_ready",
                    css_class="col-md-3",
                ),
                slot(
                    "add_membership_fee_to_invoices_for_expired_members",
                    Div(
                        "add_membership_fee_to_invoices_for_expired_members",
                        css_class="col-md-3",
                    ),
                ),
                slot(
                    "enable_online_payments",
                    Div(
                        "enable_online_payments",
                        css_class="col-md-3",
                    ),
                ),
                slot(
                    "enable_square_payments",
                    Div(
                        "enable_square_payments",
                        css_class="col-md-3",
                    ),
                ),
                Div(
                    "invoice_payment_instructions",
                    css_class="col-md-6",
                ),
                Div(
                    "invoice_rounding",
                    css_class="col-md-3",
                ),
                Div(
                    "only_whole_dollar_bids",
                    css_class="col-md-3",
                ),
                Div(
                    "minimum_bid",
                    css_class="col-md-3",
                ),
                # Div(
                #     "advanced_lot_adding",
                #     css_class="col-md-3",
                # ),
                Div(
                    "auto_add_images",
                    css_class="col-md-3",
                ),
                slot(
                    "message_users_when_lots_sell",
                    Div(
                        "message_users_when_lots_sell",
                        css_class="col-md-3",
                    ),
                ),
                # Div('set_lot_winners_url', css_class='col-md-3',),
                PrependedAppendedText(
                    "tax",
                    "",
                    "%",
                    wrapper_class="col-md-3",
                ),
                Div(
                    "promote_this_auction",
                    css_class="col-md-3",
                ),
                css_class="row",
            ),
            Submit("submit", "Save", css_class="create-update-auction btn-success"),
        )

    def clean(self):
        cleaned_data = super().clean()
        use_seller_dash_lot_numbering = cleaned_data.get("use_seller_dash_lot_numbering")
        existing_instance = self.instance

        # Both registration fees are optional (see __init__): a blank submission is "no fee". The
        # columns are NOT NULL, so fold the resulting None back to the model default of 0.
        for fee_field in ("registration_fee", "registration_fee_for_club_members"):
            cleaned_data[fee_field] = cleaned_data.get(fee_field) or 0

        # When a club is selected, payments are controlled by club settings, not auction settings
        single_club = get_single_club(create=False)
        if getattr(settings, "SINGLE_CLUB_MODE", False) and single_club:
            cleaned_data["club"] = single_club
            cleaned_data["copy_users_when_copying_this_auction"] = False
            # Participant management can't be turned off in single club mode.
            if not cleaned_data.get("manage_users_through_club"):
                cleaned_data["manage_users_through_club"] = (
                    self.instance.manage_users_through_club or SINGLE_CLUB_DEFAULT_MANAGE_MODE
                )
        if cleaned_data.get("club"):
            cleaned_data["enable_online_payments"] = False
            cleaned_data["enable_square_payments"] = False
            cleaned_data["club_member_discount"] = cleaned_data.get("club_member_discount") or 0
        else:
            # The club member discount only applies to paid club members
            cleaned_data["club_member_discount"] = 0
        if cleaned_data.get("alternate_split_mode") == "club_member":
            if not cleaned_data.get("club"):
                self.add_error(
                    "alternate_split_mode",
                    "Associate this auction with a club to use the club member discount.",
                )
            else:
                # The label field is hidden in this mode; the people getting the alternate
                # split are always club members.
                cleaned_data["alternative_split_label"] = "Club member"

        if existing_instance and existing_instance.pk:
            if use_seller_dash_lot_numbering is not existing_instance.use_seller_dash_lot_numbering:
                if existing_instance.admin_checklist_lots_added:
                    self.add_error(
                        "use_seller_dash_lot_numbering", "This option cannot be changed after lots have been added."
                    )
        pattern = r"^(test|mock|trial|example)([-_]|$)|([-_])(test|mock|trial|example)([-_]|$)"
        if cleaned_data.get("promote_this_auction") and re.search(pattern, existing_instance.slug, re.IGNORECASE):
            self.add_error("promote_this_auction", "Test auctions cannot be promoted.")
        elif cleaned_data.get("promote_this_auction") and not existing_instance.admin_checklist_location_set:
            self.add_error("promote_this_auction", "Set the location before promoting this auction")
        elif cleaned_data.get(
            "promote_this_auction"
        ) and "You should remove this line and edit this section to suit your auction." in cleaned_data.get(
            "summernote_description"
        ):
            self.add_error(
                "promote_this_auction",
                "Edit the text in the rules section above before promoting this auction.  There's still placeholder text in there that needs to be removed.",
            )
        elif cleaned_data.get("promote_this_auction") and not existing_instance.created_by.userdata.is_trusted:
            self.add_error("promote_this_auction", "Your account doesn't have permission to promote auctions.")
        if cleaned_data.get("only_whole_dollar_bids"):
            minimum_bid = cleaned_data.get("minimum_bid")
            if minimum_bid is not None and minimum_bid != minimum_bid.to_integral_value():
                is_toggling_to_whole_dollar = (
                    bool(existing_instance and existing_instance.pk)
                    and not existing_instance.only_whole_dollar_bids
                    and cleaned_data.get("only_whole_dollar_bids")
                )
                if is_toggling_to_whole_dollar:
                    cleaned_data["minimum_bid"] = round_to_whole_dollar(minimum_bid)
                else:
                    self.add_error("minimum_bid", "This auction only allows whole dollar amounts.")
        if cleaned_data.get("add_membership_fee_to_invoices_for_expired_members") and not cleaned_data.get("club"):
            self.add_error(
                "add_membership_fee_to_invoices_for_expired_members",
                "Associate this auction with a club before enabling membership fees.",
            )
        return cleaned_data

    def clean_manage_users_through_club(self):
        target = self.cleaned_data.get("manage_users_through_club") or ""
        instance = self.instance
        currently_enabled = bool(instance and instance.pk and instance.manage_users_through_club)
        target_enabled = bool(target)
        # Check-in mode is an in-person concept (members are added as they arrive at the event);
        # it has no meaning for online auctions.
        if target == "checkin" and instance and instance.is_online:
            msg = "Check-in mode is only available for in-person auctions."
            raise forms.ValidationError(msg)
        if currently_enabled and not target_enabled:
            # Allow disabling only when there are no lots or invoices
            if instance and instance.pk:
                if Lot.objects.filter(auction=instance, is_deleted=False).exists():
                    msg = "Cannot disable club-managed mode: this auction already has lots."
                    raise forms.ValidationError(msg)
                if Invoice.objects.filter(auction=instance).exists():
                    msg = "Cannot disable club-managed mode: this auction already has invoices."
                    raise forms.ValidationError(msg)
        if target_enabled and not currently_enabled:
            club = self.cleaned_data.get("club") or (instance.club if instance and instance.pk else None)
            if not club:
                msg = "Associate this auction with a club before enabling this option."
                raise forms.ValidationError(msg)
            if instance and instance.pk:
                if Lot.objects.filter(auction=instance, is_deleted=False).exists():
                    msg = "This auction already has lots. Club-managed mode can only be enabled on an empty auction."
                    raise forms.ValidationError(msg)
                if Invoice.objects.filter(auction=instance).exists():
                    msg = (
                        "This auction already has invoices. Club-managed mode can only be enabled on an empty auction."
                    )
                    raise forms.ValidationError(msg)
        return target

    @staticmethod
    def _rebuild_auctiontos_from_club(auction, bidding_allowed_override=None):
        """Delete existing AuctionTOS and recreate from club members (used when enabling or re-enabling club-managed mode).

        If bidding_allowed_override is False, all created TOS records will have bidding disabled (used for check-in mode).
        If None (default), each member's own bidding_allowed setting is used.
        """
        AuctionTOS.objects.filter(auction=auction).delete()
        default_location = PickupLocation.objects.filter(auction=auction).order_by("-is_default", "pk").first()
        if default_location and auction.club_id:
            club_members = ClubMember.objects.filter(club_id=auction.club_id, is_deleted=False).order_by(
                "createdon", "pk"
            )
            for club_member in club_members:
                if not club_member.bidder_number:
                    club_member.generate_bidder_number(save=True)
                bidding = club_member.bidding_allowed if bidding_allowed_override is None else bidding_allowed_override
                AuctionTOS.objects.create(
                    user=club_member.user,
                    auction=auction,
                    pickup_location=default_location,
                    clubmember=club_member,
                    bidder_number=club_member.bidder_number,
                    bidding_allowed=bidding,
                    selling_allowed=club_member.selling_allowed,
                    is_club_member=(auction.alternate_split_mode == "club_member" and club_member.is_paid_member),
                    name=club_member.name or "",
                    email=club_member.email or "",
                    phone_number=club_member.phone_number or "",
                    address=club_member.address or "",
                    manually_added=True,
                )

    def save(self, commit=True):
        from django.db import transaction as db_transaction

        was_only_whole_dollar_bids = bool(
            self.initial.get("only_whole_dollar_bids", self.instance.only_whole_dollar_bids)
        )
        # self.initial is populated from model_to_dict(instance) by BaseModelForm.__init__
        # BEFORE _post_clean() mutates self.instance, so it holds the original DB values.
        # Never read self.instance.<field> here — it already has the new POST value.
        old_manage_value = self.initial.get("manage_users_through_club") or ""
        was_managed_through_club = bool(old_manage_value)
        was_manage_all = old_manage_value == "all"
        target_manage_value = self.cleaned_data.get("manage_users_through_club") or ""
        target_managed_through_club = bool(target_manage_value)
        target_manage_all = target_manage_value == "all"
        # self.initial["club"] is the old club pk (int/None) from model_to_dict
        was_club_id = self.initial.get("club")
        target_club = self.cleaned_data.get("club")
        target_club_id = target_club.pk if target_club else None

        enabling_club_management = (
            commit and self.instance.pk and not was_managed_through_club and target_managed_through_club
        )
        disabling_club_management = (
            commit and self.instance.pk and was_managed_through_club and not target_managed_through_club
        )
        club_changed_while_managed = (
            commit
            and self.instance.pk
            and was_managed_through_club
            and target_managed_through_club
            and target_club_id != was_club_id
        )
        # Rebuild from club when switching to "all" from "checkin" (or any non-all managed state)
        switching_to_all = (
            commit
            and self.instance.pk
            and was_managed_through_club
            and target_manage_all
            and not was_manage_all
            and not club_changed_while_managed
        )
        # Switching from "all" → "checkin": clear auto-added TOS records
        switching_to_checkin = (
            commit
            and self.instance.pk
            and was_managed_through_club
            and target_managed_through_club
            and not target_manage_all
            and was_manage_all
            and not club_changed_while_managed
        )

        if enabling_club_management:
            with db_transaction.atomic():
                locked = Auction.objects.select_for_update().get(pk=self.instance.pk)
                if Lot.objects.filter(auction=locked, is_deleted=False).exists():
                    msg = "Cannot enable club-managed mode: lots were added while the form was open."
                    raise forms.ValidationError(msg)
                if Invoice.objects.filter(auction=locked).exists():
                    msg = "Cannot enable club-managed mode: invoices were added while the form was open."
                    raise forms.ValidationError(msg)
                auction = super().save(commit=commit)
                if target_manage_all:
                    self._rebuild_auctiontos_from_club(auction)
                else:
                    # check-in mode: add all members with bidding disabled until they check in
                    self._rebuild_auctiontos_from_club(auction, bidding_allowed_override=False)
        elif disabling_club_management:
            with db_transaction.atomic():
                locked = Auction.objects.select_for_update().get(pk=self.instance.pk)
                if Lot.objects.filter(auction=locked, is_deleted=False).exists():
                    msg = "Cannot disable club-managed mode: lots were added while the form was open."
                    raise forms.ValidationError(msg)
                if Invoice.objects.filter(auction=locked).exists():
                    msg = "Cannot disable club-managed mode: invoices were added while the form was open."
                    raise forms.ValidationError(msg)
                # Clear all AuctionTOS since club-managed mode is being turned off
                AuctionTOS.objects.filter(auction=locked).delete()
                auction = super().save(commit=commit)
        elif club_changed_while_managed:
            with db_transaction.atomic():
                locked = Auction.objects.select_for_update().get(pk=self.instance.pk)
                if Lot.objects.filter(auction=locked, is_deleted=False).exists():
                    msg = "Cannot change the club while lots exist in a club-managed auction."
                    raise forms.ValidationError(msg)
                if Invoice.objects.filter(auction=locked).exists():
                    msg = "Cannot change the club while invoices exist in a club-managed auction."
                    raise forms.ValidationError(msg)
                auction = super().save(commit=commit)
                if target_manage_all:
                    self._rebuild_auctiontos_from_club(auction)
                else:
                    self._rebuild_auctiontos_from_club(auction, bidding_allowed_override=False)
        elif switching_to_all:
            with db_transaction.atomic():
                locked = Auction.objects.select_for_update().get(pk=self.instance.pk)
                auction = super().save(commit=commit)
                self._rebuild_auctiontos_from_club(auction)
        elif switching_to_checkin:
            with db_transaction.atomic():
                locked = Auction.objects.select_for_update().get(pk=self.instance.pk)
                auction = super().save(commit=commit)
                # Rebuild all members with bidding disabled; check-in enables bidding one at a time
                self._rebuild_auctiontos_from_club(auction, bidding_allowed_override=False)
        else:
            auction = super().save(commit=commit)
        if commit and not was_only_whole_dollar_bids and auction.only_whole_dollar_bids:
            lots = Lot.objects.exclude(is_deleted=True).filter(auction=auction)
            lots_to_update = []
            for lot in lots:
                lot_changed = False
                for field_name in ("reserve_price", "buy_now_price", "winning_price"):
                    value = getattr(lot, field_name)
                    if value is not None and value != value.to_integral_value():
                        setattr(lot, field_name, round_to_whole_dollar(value))
                        lot_changed = True
                if lot_changed:
                    lots_to_update.append(lot)
            if lots_to_update:
                Lot.objects.bulk_update(lots_to_update, ["reserve_price", "buy_now_price", "winning_price"])
        if (
            commit
            and auction.alternate_split_mode == "club_member"
            and (self.initial.get("alternate_split_mode") or "") != "club_member"
        ):
            # Switching to the automatic club member mode: sync the flag for everyone
            # already in the auction so paid members immediately get the alternate split.
            for tos in AuctionTOS.objects.filter(auction=auction).select_related("clubmember", "user"):
                tos.update_alternate_split_from_membership()
        return auction


class AuctionCustomFieldsForm(forms.ModelForm):
    class Meta:
        model = Auction
        fields = [
            "allow_bulk_adding_lots",
            "reserve_price",
            "buy_now",
            "use_categories",
            "use_scientific_name",
            "use_quantity_field",
            "use_donation_field",
            "use_i_bred_this_fish_field",
            "use_reference_link",
            "use_description",
            "custom_field_1",
            "custom_field_1_name",
            "use_custom_checkbox_field",
            "custom_checkbox_name",
            "use_custom_dropdown_field",
            "custom_dropdown_name",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "auction-custom-fields-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.fields["custom_field_1"].label = "Use custom text field"
        self.helper.layout = Layout(
            HTML("""<h4>Custom fields</h4>Control what information your users can enter about lots.
                <span class='text-warning'>For advanced users only!</span>
                The default settings are recommended for most auctions<br>
                <small>If you enable more than a couple extra fields here, you should disable bulk adding lots, as too many fields in the bulk adding lots form quickly becomes overwhelming for users</small><br><br>"""),
            Div(
                Div("allow_bulk_adding_lots", css_class="col-md-4"),
                Div("reserve_price", css_class="col-md-4"),
                Div("buy_now", css_class="col-md-4"),
                Div("use_categories", css_class="col-md-4"),
                Div("use_scientific_name", css_class="col-md-4"),
                Div("use_quantity_field", css_class="col-md-4"),
                Div("use_donation_field", css_class="col-md-4"),
                Div("use_i_bred_this_fish_field", css_class="col-md-4"),
                Div("use_reference_link", css_class="col-md-4"),
                Div("use_description", css_class="col-md-4"),
                Div("custom_field_1", css_class="col-md-4"),
                Div("custom_field_1_name", css_class="col-md-4"),
                Div("use_custom_checkbox_field", css_class="col-md-4"),
                Div("custom_checkbox_name", css_class="col-md-4"),
                Div("use_custom_dropdown_field", css_class="col-md-4"),
                Div("custom_dropdown_name", css_class="col-md-4"),
                css_class="row",
            ),
            Submit("submit", "Save", css_class="btn btn-success"),
        )

    def clean(self):
        cleaned_data = super().clean()
        self.custom_dropdown_auto_disabled = False
        if cleaned_data.get("custom_field_1") == "disable":
            cleaned_data["custom_field_1_name"] = ""
        if not cleaned_data.get("use_custom_checkbox_field"):
            cleaned_data["custom_checkbox_name"] = ""
        if cleaned_data.get("use_custom_dropdown_field") == "disable":
            cleaned_data["custom_dropdown_name"] = ""
        if cleaned_data.get("use_custom_dropdown_field") != "disable":
            if not cleaned_data.get("custom_dropdown_name"):
                cleaned_data["use_custom_dropdown_field"] = "disable"
                self.custom_dropdown_auto_disabled = True
            else:
                options_count = AuctionDropdown.objects.filter(auction=self.instance).count()
                if options_count < 2:
                    cleaned_data["use_custom_dropdown_field"] = "disable"
                    self.custom_dropdown_auto_disabled = True
        return cleaned_data


class CreateLotForm(forms.ModelForm):
    """Form for creating or updating of lots"""

    # Fields needed to add new species
    # species_search = forms.CharField(max_length=200, required = False)
    # species_search.help_text = "Search here for a latin or common name, or the name of a product"
    # create_new_species = forms.BooleanField(required = False)
    # new_species_name = forms.CharField(max_length=200, required = False, label="Common name")
    # new_species_name.help_text = "You can enter synonyms here, separate by commas"
    # new_species_scientific_name = forms.CharField(max_length=200, required = False, label="Scientific name")
    # new_species_scientific_name.help_text = "Enter the Latin name of this species"
    # new_species_category = ModelChoiceField(queryset=Category.objects.all().order_by('name'), required=False,label="Category")
    cloned_from = forms.IntegerField(required=False, widget=forms.HiddenInput())
    #: Set by refreshSpeciesUI() in lot_form.html when the category picker is actually on screen.
    #:
    #: Both pickers start closed, and while the category one is closed whatever it posts is a
    #: leftover from before the species was chosen -- so ``clean_species_for_auction`` overwrites
    #: it with the species' own category.  Once somebody has opened the pickers that stops being
    #: true: what is in the box is what they chose, and deriving over the top of it would revert a
    #: deliberate answer on save, silently.  Hence one bit saying which of the two situations this
    #: post is.
    category_shown = forms.BooleanField(required=False, widget=forms.HiddenInput())

    show_payment_pickup_info = forms.BooleanField(required=False, label="Show payment/pickup info")
    AUCTION_CHOICES = [
        (True, "Yes, this lot is part of a club auction"),
        (False, "No, I'm selling this lot independently"),
    ]
    part_of_auction = forms.ChoiceField(
        choices=AUCTION_CHOICES,
        widget=forms.RadioSelect,
        label="Put into an auction?",
        required=False,
    )
    LENGTH_CHOICES = [(10, "Ends in 10 days"), (21, "Ends in 21 days")]
    run_duration = forms.ChoiceField(
        choices=LENGTH_CHOICES,
        widget=forms.RadioSelect,
        label="Posting duration",
        required=False,
    )

    class Meta:
        model = Lot
        fields = (
            "reference_link",
            "relist_if_sold",
            "relist_if_not_sold",
            "lot_name",
            "i_bred_this_fish",
            "summernote_description",
            # "description",
            "quantity",
            "reserve_price",
            "species",
            "species_category",
            "auction",
            "donation",
            "shipping_locations",
            "buy_now_price",
            "show_payment_pickup_info",
            # "promoted",
            "part_of_auction",
            "other_text",
            "local_pickup",
            "payment_paypal",
            "payment_cash",
            "payment_other",
            "payment_other_method",
            "payment_other_address",
            "run_duration",
            "custom_checkbox",
            "custom_field_1",
            "custom_dropdown",
            "image_url",
        )
        exclude = ["user", "image", "image_source"]
        widgets = {
            "summernote_description": SummernoteWidget(),
            # 'species': forms.HiddenInput(),
            # 'cloned_from': forms.HiddenInput(),
            "shipping_locations": forms.CheckboxSelectMultiple(),
            "image_url": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        self.cloned_from = kwargs.pop("cloned_from")
        self.auction = kwargs.pop("auction")
        super().__init__(*args, **kwargs)
        # self.fields["description"].widget.attrs = {"rows": 3}
        # self.fields['species_category'].required = True
        self.fields["auction"].queryset = self.user.userdata.available_auctions_to_submit_lots
        if self.auction:
            if self.fields["auction"].queryset.filter(pk=self.auction.pk).exists():
                self.fields["auction"].queryset = Auction.objects.exclude(is_deleted=True).filter(pk=self.auction.pk)
        # Default auction selection:
        # try:
        #     auctions = Auction.objects.filter(lot_submission_end_date__gte=timezone.now()).filter(date_start__lte=timezone.now()).order_by('date_end')
        # #    self.fields['auction'].initial = auctions[0] # this would set a default value.  We should make users pick this manually so they don't accidentally submit to the wrong auction
        # except:
        #     # no non-ended auctions
        #     pass
        if self.instance.pk:
            # existing lot
            # set run_duration - this does not have to be super precise as it will be recalculated when the form is validated
            self.fields["run_duration"].initial = 21
            if self.instance.date_end:
                if (self.instance.date_end - self.instance.date_posted).days < 15:
                    self.fields["run_duration"].initial = 10
            self.fields[
                "show_payment_pickup_info"
            ].initial = False  # this doesn't really matter, it just gets overridden by javascript anyway
            if self.instance.auction:
                self.fields["part_of_auction"].initial = "True"
            else:
                self.fields["part_of_auction"].initial = "False"
            # if self.instance.species:
            #    self.fields['species_search'].initial = self.instance.species.common_name.split(",")[0]
        else:
            if self.cloned_from:
                clone_from_lot = Lot.objects.filter(pk=self.cloned_from, is_deleted=False).first()
                # you can only clone your lots
                if user_can_clone_lot(self.user, clone_from_lot):
                    for field, value in clone_lot_values(clone_from_lot).items():
                        self.fields[field].initial = value
                    self.fields["cloned_from"].initial = int(self.cloned_from)
            # default to making new lots part of a club auction
            self.fields["part_of_auction"].initial = "True"
            self.fields["run_duration"].initial = 10
            try:
                # try to get the last lot shipping/payment info and use that, set show_payment_pickup_info as needed
                lastLot = (
                    Lot.objects.exclude(is_deleted=True)
                    .filter(user=self.user, auction__isnull=True)
                    .latest("date_of_last_user_edit")
                )
                self.fields["show_payment_pickup_info"].initial = False
                self.fields["shipping_locations"].initial = [
                    place[0] for place in lastLot.shipping_locations.values_list()
                ]
                self.fields["local_pickup"].initial = lastLot.local_pickup
                self.fields["other_text"].initial = lastLot.other_text
                self.fields["payment_cash"].initial = lastLot.payment_cash
                self.fields["payment_paypal"].initial = lastLot.payment_paypal
                self.fields["payment_other"].initial = lastLot.payment_other
                self.fields["payment_other_method"].initial = lastLot.payment_other_method
                self.fields["payment_other_address"].initial = lastLot.payment_other_address
            except Lot.DoesNotExist:
                self.fields["show_payment_pickup_info"].initial = True
        if self.instance.auction:
            pass
        else:
            if self.auction:
                self.fields["auction"].initial = self.auction
            else:
                try:
                    # see if this user's last auction is still available
                    # UserData is auto-created when user is saved
                    lastUserAuction = self.user.userdata.last_auction_used
                    if lastUserAuction and lastUserAuction.lot_submission_end_date > timezone.now():
                        self.fields["auction"].initial = lastUserAuction
                except (AttributeError, Auction.DoesNotExist):
                    pass
        selected_auction = self.instance.auction or self.auction
        part_of_auction_selected = str(self.data.get("part_of_auction", "")).lower() in {"true", "1", "on"}
        if self.is_bound and part_of_auction_selected:
            auction_pk = self.data.get("auction")
            selected_auction = self.fields["auction"].queryset.filter(pk=auction_pk).first() if auction_pk else None
        elif self.is_bound:
            selected_auction = None
        if not selected_auction and not self.is_bound:
            initial_auction = self.fields["auction"].initial
            if initial_auction and isinstance(initial_auction, Auction):
                selected_auction = initial_auction
        self.fields["custom_dropdown"].widget = forms.Select(choices=[("", "---------")])
        self.fields["custom_dropdown"].required = False
        self.fields["custom_dropdown"].help_text = ""
        # Always rendered here, and shown or hidden by the same JavaScript that handles the other
        # per-auction fields -- this is the form where the auction is a dropdown, so what the
        # picker should do isn't known until the user picks one.  A standalone lot has no auction
        # at all, and clean_species_for_auction drops whatever was posted in that case.
        configure_species_field(self.fields, selected_auction, always_render=True, searchable=True)
        if selected_auction:
            apply_price_input_constraints(
                self.fields, ("reserve_price", "buy_now_price"), selected_auction.only_whole_dollar_bids
            )
            custom_dropdown_options = list(
                AuctionDropdown.objects.filter(auction=selected_auction)
                .order_by("createdon")
                .values_list("value", flat=True)
            )
            validated_dropdown_value = self.instance.custom_dropdown or self.fields["custom_dropdown"].initial or ""
            if validated_dropdown_value and validated_dropdown_value not in custom_dropdown_options:
                # When copying from another lot/auction, only prefill dropdown values that exist in destination options.
                validated_dropdown_value = ""
            if (
                selected_auction.use_custom_dropdown_field != "disable"
                and selected_auction.custom_dropdown_name
                and len(custom_dropdown_options) >= 2
            ):
                self.fields["custom_dropdown"].widget = forms.Select(
                    choices=[("", "---------")] + [(value, value) for value in custom_dropdown_options]
                )
                self.fields["custom_dropdown"].label = selected_auction.custom_dropdown_name
                self.fields["custom_dropdown"].required = selected_auction.use_custom_dropdown_field == "required"
                self.fields["custom_dropdown"].initial = validated_dropdown_value
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "lot-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            # Div(
            #     'species',
            #     'create_new_species',
            #     css_class='d-none',
            # ),
            # HTML("<span id='species_selection'>"),
            # HTML("<h4>Species</h4>"),
            # HTML('<div class="btn-group" role="group" aria-label="Species Selection">\
            #     <button id="useExistingSpeciesButton" type="button" onclick="useExistingSpecies();" class="btn btn-secondary selected">Use existing species</button>\
            #     <button id="createNewSpeciesButton" type="button" onclick="createNewSpecies();" class="btn btn-secondary">Create new species</button>\
            #     <button id="skipSpeciesButton" type="button" onclick="skipSpecies();" class="btn btn-secondary mr-3">Skip choosing a species</button></div><br>\
            #     <span class="text-muted">You can search for products as well as species.  If you can\'t find your exact species/morph/collection location, create a new one.<br><br></span>'),
            # Div(
            #     Div('species_search',css_class='col-md-12',),
            #     css_class='row',
            # ),
            # Div(
            #     # Div('new_species_name',css_class='col-md-4',),
            #     # Div('new_species_scientific_name',css_class='col-md-4',),
            #     # Div('new_species_category',css_class='col-md-4',),
            #     css_class='row',
            # ),
            # HTML("</span><span id='details_selection'><h4>Details</h4><br>"),
            "cloned_from",
            "image_url",
            Div(
                Div(
                    "part_of_auction",
                    css_class="col-md-5",
                ),
                Div(
                    "auction",
                    css_class="col-md-8",
                ),
                Div(
                    "run_duration",
                    css_class="col-md-4",
                ),
                Div(
                    "relist_if_not_sold",
                    css_class="col-md-4",
                ),
                Div(
                    "relist_if_sold",
                    css_class="col-md-4",
                ),
                # Div(
                #     "promoted",
                #     css_class="col-md-4",
                # ),
                Div(
                    "show_payment_pickup_info",
                    css_class="col-md-12",
                ),
                Div(
                    "lot_name",
                    # What the lot name was understood as, and the way back to the controls.  Both
                    # pickers below start hidden and refreshSpeciesUI() in lot_form.html decides
                    # what this says: the scientific name when one was identified, the category
                    # when it wasn't.  Rendered here, under the name it is talking about.
                    HTML(
                        "<div id='species-summary' class='form-text text-muted mb-3 d-none'>"
                        "<span id='species-summary-text'></span>"
                        "<button type='button' id='species-edit' class='btn btn-link btn-sm p-0 ms-1 align-baseline'>"
                        "Change</button></div>"
                    ),
                    css_class="col-md-12",
                ),
                Div(
                    "species",
                    css_class="col-md-12",
                ),
                # Directly under the scientific name, and shown and hidden with it: they are two
                # halves of one question ("what is this?"), and a category picker that appears on
                # its own halfway down the form reads as an unrelated chore.
                Div(
                    "species_category",
                    css_class="col-md-12",
                ),
                "category_shown",
                Div(
                    "custom_field_1",
                    css_class="col-md-12",
                ),
                Div(
                    "summernote_description",
                    css_class="col-md-12",
                ),
                Div(
                    "reference_link",
                    css_class="col-md-12",
                ),
                Div(
                    "quantity",
                    css_class="col-md-3",
                ),
                Div(
                    "i_bred_this_fish",
                    css_class="col-md-3",
                ),
                Div(
                    "custom_checkbox",
                    css_class="col-md-3",
                ),
                Div(
                    "custom_dropdown",
                    css_class="col-md-3",
                ),
                Div(
                    "reserve_price",
                    css_class="col-md-3",
                ),
                Div(
                    "buy_now_price",
                    css_class="col-md-3",
                ),
                Div(
                    "donation",
                    css_class="col-md-3",
                ),
                css_class="row",
            ),
            HTML("<span id='payment_pickup_info'><h4>Payment/pickup info</h4><br>"),
            Div(
                Div(
                    "local_pickup",
                    css_class="col-md-6",
                ),
                Div(
                    "shipping_locations",
                    css_class="col-md-6",
                ),
                Div(
                    "other_text",
                    css_class="col-md-12",
                ),
                Div(
                    "payment_cash",
                    css_class="col-md-4",
                ),
                Div(
                    "payment_paypal",
                    css_class="col-md-4",
                ),
                Div(
                    "payment_other",
                    css_class="col-md-4",
                ),
                Div(
                    "payment_other_method",
                    css_class="col-md-4",
                ),
                Div(
                    "payment_other_address",
                    css_class="col-md-8",
                ),
                css_class="row",
            ),
            HTML("</span>"),
            Submit("submit", "Save", css_class="create-update-lot btn-success mt-1 mr-1"),
            HTML("</span>"),
        )

    def clean(self):
        cleaned_data = super().clean()
        # create_new_species = cleaned_data.get("create_new_species")
        # new_species_name = cleaned_data.get("new_species_name")
        # new_species_scientific_name = cleaned_data.get("new_species_scientific_name")
        # new_species_category = cleaned_data.get("new_species_category")
        # if create_new_species:
        #     if not new_species_name:
        #         self.add_error('new_species_name', "Enter the common name of the new species to create")
        #     if not new_species_scientific_name:
        #         self.add_error('new_species_scientific_name', "Enter the scientific name of the new species to create")
        #     if not new_species_category:
        #         self.add_error("new_species_category", "Pick a category")

        # this is now handled more seamlessly in LotValidation.form_valid -- the user can always edit it later
        # image = cleaned_data.get("image")
        # image_source = cleaned_data.get("image_source")
        # if image and not image_source:
        #    self.add_error('image_source', "Is this your picture?")

        # this doesn't really matter either - if the user screws with the client side validation, the lot simply won't be available
        auction = cleaned_data.get("auction")
        part_of_auction = cleaned_data.get("part_of_auction")
        clean_species_for_auction(
            cleaned_data,
            auction if part_of_auction == "True" else None,
            # Only when the picker was closed, which is when what it posted is a leftover rather
            # than an answer.  See category_shown.
            derive_category=not cleaned_data.get("category_shown"),
            instance=self.instance,
        )
        if part_of_auction == "True":
            if auction is None:
                self.add_error("auction", "Select an auction")
        else:
            # set auction to empty
            cleaned_data["auction"] = None
            auction = None
            if not self.user.userdata.can_submit_standalone_lots:
                self.add_error("part_of_auction", "This feature is not enabled for your account")
            if not cleaned_data.get("shipping_locations") and not cleaned_data.get("local_pickup"):
                self.add_error(
                    "show_payment_pickup_info",
                    "Select local pickup and/or a location to ship to",
                )
            if (
                not cleaned_data.get("payment_cash")
                and not cleaned_data.get("payment_paypal")
                and not cleaned_data.get("payment_other")
            ):
                self.add_error("show_payment_pickup_info", "Select at least one payment method")
            if cleaned_data.get("payment_other") and not cleaned_data.get("payment_other_method"):
                self.add_error("payment_other_method", "Enter your payment method")
        if auction:
            custom_dropdown_options = list(
                AuctionDropdown.objects.filter(auction=auction).values_list("value", flat=True)
            )
            selected_dropdown = cleaned_data.get("custom_dropdown", "")
            if (
                auction.use_custom_dropdown_field != "disable"
                and auction.custom_dropdown_name
                and len(custom_dropdown_options) >= 2
            ):
                if auction.use_custom_dropdown_field == "required" and not selected_dropdown:
                    self.add_error("custom_dropdown", f"{auction.custom_dropdown_name} is required")
                if selected_dropdown and selected_dropdown not in custom_dropdown_options:
                    self.add_error("custom_dropdown", "Select a valid option")
            else:
                cleaned_data["custom_dropdown"] = ""
            if auction.only_whole_dollar_bids:
                reserve_price = cleaned_data.get("reserve_price")
                if reserve_price is not None and reserve_price != reserve_price.to_integral_value():
                    self.add_error("reserve_price", "This auction only allows whole dollar amounts.")
                buy_now_price = cleaned_data.get("buy_now_price")
                if buy_now_price is not None and buy_now_price != buy_now_price.to_integral_value():
                    self.add_error("buy_now_price", "This auction only allows whole dollar amounts.")
            auctiontos = AuctionTOS.objects.filter(user=self.user.pk, auction=auction).first()
            if not auctiontos:
                self.add_error("auction", "You need to join this auction before you can add lots")
            else:
                if not auctiontos.selling_allowed:
                    self.add_error(
                        "auction",
                        "You don't have permission to sell lots in this auction",
                    )
            try:
                UserBan.objects.get(banned_user=self.user.pk, user=auction.created_by.pk)
                self.add_error("auction", "You've been banned from selling lots in this auction")
            except UserBan.DoesNotExist:
                pass
            # thisAuction = Auction.objects.get(pk=auction)
            if not self.instance.pk:  # only run this check when creating a lot, not when editing
                if auction.max_lots_per_user:
                    if auction.allow_additional_lots_as_donation:
                        numberOfLots = (
                            Lot.objects.exclude(is_deleted=True)
                            .filter(
                                user=self.user,
                                auction=auction,
                                donation=False,
                                banned=False,
                            )
                            .count()
                        )
                    else:
                        numberOfLots = (
                            Lot.objects.exclude(is_deleted=True)
                            .filter(user=self.user, auction=auction, banned=False)
                            .count()
                        )
                    if numberOfLots >= auction.max_lots_per_user:
                        if auction.allow_additional_lots_as_donation:
                            if not cleaned_data.get("donation"):
                                self.add_error(
                                    "donation",
                                    f"You've already added {auction.max_lots_per_user} lots to this auction.  You can add more lots as a donation.",
                                )
                        else:
                            self.add_error(
                                "auction",
                                f"You can't add more lots to this auction (Limit: {auction.max_lots_per_user})",
                            )
            else:
                # that special case when someone is editing a lot to get around the limit
                is_saved = Lot.objects.filter(pk=self.instance.pk, donation=True).first()
                if is_saved and auction.allow_additional_lots_as_donation and not cleaned_data.get("donation"):
                    lot_count = (
                        Lot.objects.exclude(is_deleted=True)
                        .filter(
                            auction=auction,
                            user=self.user,
                            donation=False,
                            banned=False,
                        )
                        .count()
                    )
                    if auction.max_lots_per_user and lot_count >= auction.max_lots_per_user:
                        self.add_error(
                            "donation",
                            "This needs to be a donation due to the max lots per user allowed in this auction",
                        )
        else:
            cleaned_data["custom_dropdown"] = ""

        # check to see if this lot exists already
        # this code is no longer needed since we disable the submit button on click; if there start being problems with duplicate lots, I'll uncomment the below
        # try:
        #     existingLot = Lot.objects.exclude(is_deleted=True).filter(user=self.user, lot_name=cleaned_data.get("lot_name"), description=cleaned_data.get("description"), active = True).exclude(pk=self.instance.pk)
        #     if existingLot:
        #         self.add_error('description', "You've already added a lot exactly like this.  If you mean to submit another lot, change something here so it's unique")
        # except:
        #     pass
        return cleaned_data


class CustomSignupForm(SignupForm):
    """To require firstname and lastname when signing up"""

    first_name = forms.CharField(max_length=30, label="First Name")
    last_name = forms.CharField(max_length=30, label="Last Name")
    captcha = ReCaptchaField(widget=ReCaptchaV2Invisible)

    field_order = ["email", "first_name", "last_name", "username", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not recaptcha_is_configured():
            self.fields.pop("captcha", None)
            logger.debug("reCAPTCHA is not configured; removing captcha from the signup form.")

    def signup(self, request, user):
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.save()
        return user


class CustomResetPasswordForm(ResetPasswordForm):
    captcha = ReCaptchaField(widget=ReCaptchaV2Invisible)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not recaptcha_is_configured():
            self.fields.pop("captcha", None)
            logger.debug("reCAPTCHA is not configured; removing captcha from the password reset form.")


class UserLocation(forms.ModelForm):
    """
    We need to have a form based on userdata in order to set the latitude and longitude correctly.
    But from a user's standpoint, it makes sense to set their name on the same form
    """

    first_name = forms.CharField(max_length=30, label="First name", required=True)
    last_name = forms.CharField(max_length=150, label="Last name", required=True)
    club_affiliation = forms.CharField(max_length=100, label="Club", required=False)
    club_affiliation.help_text = "Optional.  If you belong to a club, enter the name here."

    class Meta:
        model = UserData
        fields = (
            "phone_number",
            "location",
            "location_coordinates",
            "address",
            "club",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["address"].widget = forms.Textarea()
        self.fields["address"].widget.attrs = {"rows": 3}
        self.fields["address"].required = True
        self.fields[
            "location"
        ].help_text = "Optional. You'll be notified about new lots that can ship to this location."
        self.fields["phone_number"].help_text = "Optional"
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "user-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.fields["club"].widget = HiddenInput()
        if self.instance.club:
            self.fields["club_affiliation"].initial = self.instance.club.name
        self.helper.layout = Layout(
            "club",
            Div(
                Div(
                    "first_name",
                    css_class="col-md-6",
                ),
                Div(
                    "last_name",
                    css_class="col-md-6",
                ),
                css_class="row",
            ),
            Div(
                Div(
                    "phone_number",
                    css_class="col-md-4",
                ),
                Div(
                    "location",
                    css_class="col-md-3",
                ),
                Div(
                    "club_affiliation",
                    css_class="col-md-5",
                ),
                css_class="row",
            ),
            Div(
                Div("address"),
                Div("location_coordinates"),
            ),
            Submit("submit", "Save", css_class="btn-success"),
        )


class ChangeUsernameForm(forms.ModelForm):
    """Needed to allow users to change their username"""

    class Meta:
        model = User
        fields = ("username",)
        exclude = (
            "last_login",
            "is_superuser",
            "groups",
            "is_staff",
            "is_active",
            "date_joined",
            "email",
            "user_permissions",
        )

    def clean_username(self):
        username = self.cleaned_data.get("username", "")
        validate_username_no_at_symbol(username)
        return username

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "user-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            Div(
                Div(
                    "username",
                    css_class="col-md-6",
                ),
                css_class="row",
            ),
            Submit("submit", "Save", css_class="btn-success"),
        )


class DisabledOptionSelect(forms.Select):
    """A ``<select>`` that renders specific option values as ``disabled``.

    The options are still shown (so the user can see the choice exists) but can't be picked. Used for
    print methods that only work in the native app when the page is viewed on the web.
    """

    def __init__(self, *args, disabled_values=(), **kwargs):
        self.disabled_values = {str(v) for v in disabled_values}
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, *args, **kwargs):
        option = super().create_option(name, value, *args, **kwargs)
        if str(option["value"]) in self.disabled_values:
            option["attrs"]["disabled"] = True
        return option


class UserLabelPrefsForm(forms.ModelForm):
    class Meta:
        model = UserLabelPrefs
        exclude = ("user",)

    def __init__(self, *args, show_print_method=True, is_mobile_app=False, show_print_from_computer=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "printing-prefs"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        # The print-method dropdown is the primary choice on the page, but "System printer" /
        # "Bluetooth" only mean anything in the app, so it's hidden for pure-web users who have no
        # device (see UserLabelPrefsView). When hidden, drop the field so the form leaves it as-is.
        print_method_layout = []
        if show_print_method:
            if not is_mobile_app:
                # On the web those two methods do nothing (they need the native app), so show them
                # disabled with a note that only PDF works from a browser. PDF stays selectable, and a
                # value the user set in the app (e.g. bluetooth) is preserved on save.
                self.fields["print_method"].widget = DisabledOptionSelect(
                    choices=UserLabelPrefs.PRINT_METHODS,
                    disabled_values=("system", "bluetooth"),
                )
                self.fields["print_method"].help_text = (
                    "System printer and Bluetooth printing only work in the FishAuctions app. "
                    "Only PDF labels are available from the web."
                )
            print_method_layout = [
                Div(
                    Div("print_method", css_class="col-sm-7"),
                    css_class="row",
                ),
                # Warnings alert + (in-app) Bluetooth connect card + the live-warning JS map. Kept in
                # a template so the UX/copy iterates server-side without an app release.
                HTML('{% include "printing_extras.html" %}'),
            ]
        else:
            del self.fields["print_method"]
        # "Print from my computer to my phone" is only shown to an account with a phone that has ever
        # reported a paired printer -- otherwise it is a switch with nothing behind it. Dropped from
        # the form entirely when hidden, so a save from a browser that has never seen it leaves the
        # stored value alone.
        print_from_computer_layout = []
        if show_print_from_computer:
            print_from_computer_layout = [
                Div(
                    Div("print_from_computer", css_class="col-sm-12"),
                    css_class="row",
                ),
                # The "your phone was last seen…" line. In a template because it is the one fact that
                # decides whether the feature will work at all, and the copy for it wants to change
                # without a form edit.
                HTML('{% include "printing_remote_extras.html" %}'),
            ]
        else:
            del self.fields["print_from_computer"]
        self.helper.layout = Layout(
            *print_method_layout,
            *print_from_computer_layout,
            Div(
                Div(
                    "preset",
                    css_class="col-sm-7",
                ),
                Div(
                    "empty_labels",
                    css_class="col-sm-3",
                ),
                Div(
                    "print_border",
                    css_class="col-sm-2",
                ),
                css_class="row",
            ),
            Div(
                HTML(
                    """<div class="alert alert-warning text-dark" role="alert"><i class="bi bi-exclamation-triangle me-1"></i>You most likely do not need to change these settings!  Some combinations may not work, so if you have a problem, just leave a comment <a href="https://github.com/iragm/fishauctions/issues/122" class="alert-link">here</a> and I'll fix it.</div>"""
                ),
                Div(
                    Div(
                        "page_width",
                        css_class="col-md-6",
                    ),
                    Div(
                        "page_height",
                        css_class="col-md-6",
                    ),
                    css_class="row",
                ),
                Div(
                    Div(
                        "page_margin_top",
                        css_class="col-lg-3",
                    ),
                    Div(
                        "page_margin_bottom",
                        css_class="col-lg-3",
                    ),
                    Div(
                        "page_margin_left",
                        css_class="col-lg-3",
                    ),
                    Div(
                        "page_margin_right",
                        css_class="col-lg-3",
                    ),
                    css_class="row",
                ),
                Div(
                    Div(
                        "label_width",
                        css_class="col-lg-3",
                    ),
                    Div(
                        "label_height",
                        css_class="col-lg-3",
                    ),
                    Div(
                        "label_margin_right",
                        css_class="col-lg-3",
                    ),
                    Div(
                        "label_margin_bottom",
                        css_class="col-lg-3",
                    ),
                    css_class="row",
                ),
                Div(
                    Div(
                        "font_size",
                        css_class="col-md-6",
                    ),
                    Div(
                        "unit",
                        css_class="col-md-6",
                    ),
                    css_class="row",
                ),
                id="custom_form",
            ),
            Submit("submit", "Save", css_class="btn-success"),
        )


class ChangeUserPreferencesForm(forms.ModelForm):
    class Meta:
        model = UserData
        fields = (
            "email_visible",
            "distance_unit",
            "preferred_currency",
            "email_me_about_new_auctions",
            "email_me_about_new_auctions_distance",
            "email_me_about_new_local_lots",
            "local_distance",
            "email_me_about_new_lots_ship_to_location",
            "email_me_when_people_comment_on_my_lots",
            "email_me_about_new_chat_replies",
            "push_notifications_instead_of_email",
            "email_me_about_new_in_person_auctions",
            "email_me_about_new_in_person_auctions_distance",
            "send_reminder_emails_about_joining_auctions",
            "username_visible",
            "share_lot_images",
            "auto_add_images",
            "push_notifications_when_lots_sell",
            "show_nearby_auctions",
        )

    def __init__(self, user, *args, is_mobile_app=False, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        # Convert distances from miles to km for display if user prefers km
        if self.instance and self.instance.distance_unit == "km":
            if self.instance.email_me_about_new_auctions_distance:
                self.initial["email_me_about_new_auctions_distance"] = round(
                    self.instance.email_me_about_new_auctions_distance * MILES_TO_KM
                )
            if self.instance.email_me_about_new_in_person_auctions_distance:
                self.initial["email_me_about_new_in_person_auctions_distance"] = round(
                    self.instance.email_me_about_new_in_person_auctions_distance * MILES_TO_KM
                )
            if self.instance.local_distance:
                self.initial["local_distance"] = round(self.instance.local_distance * MILES_TO_KM)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_id = "user-form"
        self.helper.form_class = "form"
        self.helper.form_tag = True
        self.subscriptions = (
            ChatSubscription.objects.exclude(lot__user=self.user).filter(user=self.user, unsubscribed=False).count()
        )
        self.fields[
            "email_me_about_new_chat_replies"
        ].help_text = f"Only for lots that don't belong to you.  Unchecking this will turn off notifications for {self.subscriptions} lot(s) you've already commented on."
        # Push notifications need a signed-in app install with a live FCM token. Always show the
        # toggle so users know it exists, but disable it (with an explanatory note) when there's no
        # device to push to — disabling keeps the stored value unchanged on save.
        if not (self.instance and self.instance.pk and self.instance.has_push_device):
            self.fields["push_notifications_instead_of_email"].disabled = True
            # Someone whose phone has gone quiet needs a different sentence than someone who never
            # had the app: the box stays ticked (their stored choice is untouched, so reinstalling
            # just resumes push) and telling them to "enable this" would be nonsense. A device row
            # outlives an uninstall -- only the token is cleared -- so it's what tells them apart.
            had_the_app = self.instance and self.instance.pk and MobileDevice.objects.filter(user=user).exists()
            if had_the_app:
                self.fields["push_notifications_instead_of_email"].help_text = (
                    "Your phone isn't receiving notifications right now -- the app was removed, signed "
                    "out, or has notifications turned off -- so we're emailing you instead. Reinstall "
                    "the app and sign in to pick up where you left off."
                )
            else:
                self.fields["push_notifications_instead_of_email"].help_text = (
                    "Install the FishAuctions app and sign in on a device to enable this. Then you'll get "
                    "notifications in the app instead of emails, for everything except account emails."
                )
        # The watched-lot "bidding is starting" alert goes to the app whenever the app can receive
        # it (see notify_watchers_lot_selling_soon), so for those users the browser-subscribe prompt
        # this field's help text carries would point at the wrong device. There's nothing to
        # subscribe to inside the app's own WebView either -- it has no Push API.
        has_app_push = bool(self.instance and self.instance.pk and self.instance.has_app_push)
        self.can_subscribe_to_webpush = not has_app_push and not is_mobile_app
        if has_app_push:
            self.fields["push_notifications_when_lots_sell"].help_text = (
                "For in-person auctions, get a notification when bidding starts on a lot that you've "
                "watched.  These go to the app on your phone -- including lots you watch here on the "
                "website -- so you won't also get a browser notification."
            )
        elif is_mobile_app:
            self.fields["push_notifications_when_lots_sell"].help_text = (
                "For in-person auctions, get a notification when bidding starts on a lot that you've "
                "watched.  Allow notifications for this app to receive them."
            )
        # Update help text for distance fields based on selected unit
        unit = "km" if self.instance and self.instance.distance_unit == "km" else "miles"
        self.fields["email_me_about_new_auctions_distance"].help_text = f"{unit}, from your address"
        self.fields["email_me_about_new_in_person_auctions_distance"].help_text = f"{unit}, from your address"
        self.fields["local_distance"].help_text = f"{unit}, from your address"
        local_lots_fields = []
        if settings.ALLOW_USERS_TO_CREATE_LOTS:
            local_lots_fields = [
                Div(
                    Div(
                        "email_me_about_new_local_lots",
                        css_class="col-md-8",
                    ),
                    Div(
                        "local_distance",
                        css_class="col-md-4",
                    ),
                    css_class="row",
                ),
                Div(
                    Div(
                        "email_me_about_new_lots_ship_to_location",
                        css_class="col-md-12",
                    ),
                    css_class="row",
                ),
            ]
        self.helper.layout = Layout(
            Div(
                Div(
                    "email_visible",
                    css_class="col-md-4",
                ),
                Div(
                    "username_visible",
                    css_class="col-md-4",
                ),
                Div(
                    "share_lot_images",
                    css_class="col-md-6",
                ),
                Div(
                    "auto_add_images",
                    css_class="col-md-6",
                ),
                # Div('use_list_view',css_class='col-md-4',),
                # Div('use_dark_theme',css_class='col-md-4',),
                Div(
                    "push_notifications_when_lots_sell",
                    css_class="col-md-6",
                ),
                Div(
                    "distance_unit",
                    css_class="col-md-4",
                ),
                Div(
                    "preferred_currency",
                    css_class="col-md-4",
                ),
                css_class="row",
            ),
            Div(
                Div(
                    "show_nearby_auctions",
                    css_class="col-md-12",
                ),
                css_class="row",
            ),
            HTML('<h4 class="mt-4">Notifications</h4>'),
            Div(
                Div(
                    "push_notifications_instead_of_email",
                    css_class="col-md-12",
                ),
                css_class="row",
            ),
            Div(
                Div(
                    "email_me_when_people_comment_on_my_lots",
                    css_class="col-md-4",
                ),
                Div(
                    "email_me_about_new_chat_replies",
                    css_class="col-md-4",
                ),
                Div(
                    "send_reminder_emails_about_joining_auctions",
                    css_class="col-md-4",
                ),
                css_class="row",
            ),
            HTML(
                "<p class=\"text-muted small\">You'll get one email per week that contains an update on everything you've"
                " checked below, and only if you haven't visited the site in the last 6 days.</p>"
            ),
            Div(
                Div(
                    "email_me_about_new_auctions",
                    css_class="col-md-8",
                ),
                Div(
                    "email_me_about_new_auctions_distance",
                    css_class="col-md-4",
                ),
                css_class="row",
            ),
            Div(
                Div(
                    "email_me_about_new_in_person_auctions",
                    css_class="col-md-8",
                ),
                Div(
                    "email_me_about_new_in_person_auctions_distance",
                    css_class="col-md-4",
                ),
                css_class="row",
            ),
            *local_lots_fields,
            # Div(
            #     Div('location',css_class='col-md-6',),
            #
            #     css_class='row',
            # ),
            Submit("submit", "Save", css_class="btn-success"),
        )

    def clean(self):
        cleaned_data = super().clean()
        # Convert distance values from km to miles if needed, as we store everything in miles
        distance_unit = cleaned_data.get("distance_unit")
        if distance_unit == "km":
            # Convert km to miles for storage
            if cleaned_data.get("email_me_about_new_auctions_distance"):
                cleaned_data["email_me_about_new_auctions_distance"] = round(
                    cleaned_data["email_me_about_new_auctions_distance"] / MILES_TO_KM
                )
            if cleaned_data.get("email_me_about_new_in_person_auctions_distance"):
                cleaned_data["email_me_about_new_in_person_auctions_distance"] = round(
                    cleaned_data["email_me_about_new_in_person_auctions_distance"] / MILES_TO_KM
                )
            if cleaned_data.get("local_distance"):
                cleaned_data["local_distance"] = round(cleaned_data["local_distance"] / MILES_TO_KM)
        return cleaned_data


class LabelPrintFieldsForm(forms.Form):
    lot_number = forms.BooleanField(
        label="Lot number",
        help_text="Lot number is required",
        disabled=True,
        required=False,
        initial=True,
    )

    def __init__(self, *args, **kwargs):
        self.auction = kwargs.pop("auction", None)
        super().__init__(*args, **kwargs)

        self.available_fields = [
            # if updating this:
            # also update models.Auction.label_print_fields if a new field should be enabled by default
            # update views.LotLabelView.get_context_data and put the field in either the first or second column
            {
                "value": "qr_code",
                "description": "QR Code",
                "tooltip": "Contains a link to view each lot.  Use your phone's camera to scan.",
            },
            {
                "value": "lot_name",
                "description": "Lot name",
                "tooltip": "<span class='text-warning'>Recommended</span>, otherwise people may put the label on the wrong lot",
            },
            {
                "value": "scientific_name",
                "description": "Scientific name",
                "tooltip": "Scientific name is disabled in this auction, this will not do anything"
                if not self.auction.use_scientific_name
                else "Only printed on lots where the seller picked a species",
            },
            {"value": "category", "description": "Category", "tooltip": ""},
            {
                "value": "donation_label",
                "description": "Donation",
                "tooltip": "Mark (D) on any lots that are a donation",
            },
            {
                "value": "min_bid_label",
                "description": "Minimum bid",
                "tooltip": "Min bid is disabled in this auction, this will not do anything"
                if self.auction.reserve_price == "disable"
                else "Will only be displayed if the lot has a minimum bid set. <span class='text-warning'>Recommended</span>",
            },
            {
                "value": "buy_now_label",
                "description": "Buy now price",
                "tooltip": "Buy now is disabled in this auction, this will not do anything"
                if self.auction.buy_now == "disable"
                else "Will only be displayed if the lot has a buy now price set. <span class='text-warning'>Recommended</span>",
            },
            {
                "value": "custom_field_1",
                "description": "Custom text field"
                if not self.auction.custom_field_1_name
                else self.auction.custom_field_1_name,
                "tooltip": "Custom text field is disabled in this auction, this will not do anything"
                if self.auction.custom_field_1 == "disable"
                else "",
            },
            {
                "value": "custom_checkbox_label",
                "description": "Custom checkbox"
                if not self.auction.custom_checkbox_name
                else self.auction.custom_checkbox_name,
                "tooltip": "Custom checkbox is disabled in this auction, this will not do anything"
                if not self.auction.use_custom_checkbox_field
                else "",
            },
            {
                "value": "custom_dropdown_label",
                "description": "Custom dropdown"
                if not self.auction.custom_dropdown_name
                else self.auction.custom_dropdown_name,
                "tooltip": "Custom dropdown is disabled in this auction, this will not do anything"
                if self.auction.use_custom_dropdown_field == "disable" or not self.auction.custom_dropdown_name
                else "",
            },
            {
                "value": "i_bred_this_fish_label",
                "description": "Breeder points",
                "tooltip": "Breeder points field is disabled in this auction, this will not do anything"
                if not self.auction.use_i_bred_this_fish_field
                else "",
            },
            {"value": "quantity_label", "description": "Quantity", "tooltip": ""},
            {
                "value": "auction_date",
                "description": "Auction date",
                "tooltip": f"For record keeping of when lots were acquired, show auction date.  It will appear as {self.auction.date_start.strftime('%b %Y')}",
            },
            {"value": "seller_name", "description": "Seller's name", "tooltip": ""},
            {
                "value": "seller_email",
                "description": "Seller's email",
                "tooltip": "Not recommended, this allows buyers to contact the seller directly.  The club should mediate disputes.",
            },
            {
                "value": "description_label",
                "description": "Description",
                "tooltip": "Not recommended, as descriptions can be very long.",
            },
        ]

        label_print_fields = self.auction.label_print_fields if self.auction else ""
        selected_fields = label_print_fields.split(",")

        # Iterate over available_fields and create form fields
        for field in self.available_fields:
            field_value = field["value"]
            self.fields[field_value] = forms.BooleanField(
                label=field["description"],
                required=False,
                initial=field_value in selected_fields,
                help_text=field.get("tooltip", ""),
            )

        # Set up Crispy Form helper
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.layout = Layout(
            HTML("<h4>Select fields to print:</h4>"),
            Field("lot_number"),
            Div(*[Field(field["value"]) for field in self.available_fields]),  # Use field['value']
            Submit("save", "Save", css_class="btn btn-success"),  # Save button
        )

    def save(self):
        selected_fields = [field["value"] for field in self.available_fields if self.cleaned_data.get(field["value"])]
        self.auction.label_print_fields = ",".join(selected_fields)
        self.auction.save()


class MarksClubMemberAdminEditedMixin:
    """Saving one of the club admin's member forms hands the record to the club.

    ``ClubMember.admin_edited`` decides what happens to a row when the person deletes their site
    account: one an admin has created or edited is the club's own record and keeps its details,
    losing only the account link (see :mod:`auctions.account_deletion`). Every form using this mixin
    is reachable only with club admin permissions, so a save through one is exactly that event.
    Deliberately not used by ClubMemberSelfServiceForm, which is the member editing themselves.
    """

    def save(self, commit=True):
        self.instance.admin_edited = True
        return super().save(commit=commit)


class ClubMemberSelfServiceForm(forms.ModelForm):
    """Form for club members to update their own contact info."""

    class Meta:
        model = ClubMember
        fields = ["name", "phone_number", "address"]


class ClubEventForm(forms.ModelForm):
    """Add or edit an event on a club's calendar.

    Kept deliberately short — title and a start time are the only things required, everything
    else is optional, so posting a meeting takes a few seconds.

    On a **generated** event (an auction, or one of its pickup times) the form narrows itself to
    the title and the description, because those are the only two things a club owns there. A
    club's monthly meeting is often the auction, and "In-person auction." is not what they want
    members reading on their phone — but the dates, the location and whether the event exists at
    all belong to the auction, and an event whose date disagrees with its auction is worse than no
    feature at all. Typing either field sets the matching ``*_is_custom`` flag, which is what stops
    ``club_events.sync_one_auction_event`` writing over it on the auction's next save; the reset
    box clears the flag and puts the generated wording straight back.
    """

    reset_title = forms.BooleanField(required=False, label="Use the auction's title instead")
    reset_description = forms.BooleanField(required=False, label="Use the auction's description instead")

    class Meta:
        model = ClubEvent
        fields = ["title", "date_start", "date_end", "location", "description", "cancelled"]
        widgets = {
            "date_start": DateTimePickerInput(),
            "date_end": DateTimePickerInput(),
            "location": forms.TextInput(attrs={"placeholder": "123 Main St, Springfield"}),
            "description": forms.Textarea(attrs={"rows": 4}),
        }
        help_texts = {
            "title": "For example: Monthly meeting, or Spring swap meet.",
            "date_end": "Optional. Left blank, the event is assumed to run two hours.",
            "description": "Optional. Shown on your club page, in Google Calendar, and in Discord.",
        }

    def __init__(self, *args, **kwargs):
        user_timezone = kwargs.pop("user_timezone", None)
        if user_timezone:
            # The form is rendered inside base.html's {% timezone %} block, so an admin sees these
            # times in their own timezone. Read them back the same way, or every save shifts the
            # event by the difference between their timezone and the site's.
            timezone.activate(user_timezone)
        super().__init__(*args, **kwargs)
        self.fields["date_end"].required = False
        is_edit = bool(self.instance and self.instance.pk)
        self.is_generated = bool(is_edit and self.instance.is_automatic)
        if self.is_generated:
            # Imported here rather than at the top: club_events pulls in google_calendar and
            # discord_events, and forms.py is imported early enough that doing it up there is
            # asking for an import cycle the day one of those wants a form.
            from auctions import club_events

            self.generated_title, self.generated_description = club_events.generated_wording(self.instance)
        else:
            self.generated_title, self.generated_description = "", ""
        self.helper = FormHelper()
        self.helper.form_method = "post"
        if self.is_generated:
            layout_fields = self._narrow_to_the_wording()
        else:
            layout_fields = [
                "title",
                Div(
                    Div("date_start", css_class="col-md-6"),
                    Div("date_end", css_class="col-md-6"),
                    css_class="row",
                ),
                "location",
                "description",
            ]
            if is_edit:
                # Only worth offering once the event exists — you don't add an event to call it off.
                layout_fields.append("cancelled")
            else:
                del self.fields["cancelled"]
            del self.fields["reset_title"]
            del self.fields["reset_description"]
        self.helper.layout = Layout(*layout_fields)
        self.helper.add_input(Submit("submit", "Save event", css_class="btn-primary"))

    def _narrow_to_the_wording(self):
        """Drop every field the auction owns, and label the two that are left."""
        for name in ("date_start", "date_end", "location", "cancelled"):
            del self.fields[name]
        # Both stay required exactly as the model has them — a generated event with a blank title
        # would show up blank in every member's calendar.
        title_field = self.fields["title"]
        title_field.help_text = (
            f"What members see on their calendar. The auction's own title is “{self.generated_title}”."
        )
        self.fields["description"].help_text = (
            "The details that change from one meeting to the next — doors at 6:30, bring a dish, "
            f"who's speaking. Replaces “{self.generated_description}”."
        )
        for name in ("reset_title", "reset_description"):
            self.fields[name].help_text = "Tick to go back to what the auction says, now and from now on."
        return ["title", "reset_title", "description", "reset_description"]

    def clean(self):
        cleaned_data = super().clean()
        start = cleaned_data.get("date_start")
        end = cleaned_data.get("date_end")
        if start and end and end <= start:
            self.add_error("date_end", "The end time has to be after the start time.")
        return cleaned_data

    def save(self, commit=True):
        """Record which of the two fields the club typed, so the next sync leaves them alone."""
        event = super().save(commit=False)
        if self.is_generated:
            for field, reset, generated in (
                ("title", "reset_title", self.generated_title),
                ("description", "reset_description", self.generated_description),
            ):
                if self.cleaned_data.get(reset):
                    # Reset wins over anything typed in the box above it: somebody who ticks it and
                    # edits the text in the same save has said two things, and this is the one they
                    # can't get back to any other way.
                    setattr(event, field, generated)
                    setattr(event, f"{field}_is_custom", False)
                else:
                    # Typing the generated wording back in by hand is not a custom value — there
                    # would be nothing for the flag to protect.
                    setattr(event, f"{field}_is_custom", getattr(event, field) != generated)
        if commit:
            event.save()
        return event


class ClubAnnouncementForm(forms.ModelForm):
    """Say one thing to a club's members, in as many places at once as the club has set up.

    The checkboxes are the whole design: an announcement isn't a channel, it's a message, and the
    club decides per message whether it goes to the people in Discord, the people with the app, the
    club's mailing list, or the club's own website. Each one is offered honestly — Discord is
    switched off with a reason when there is no channel to post in, and the push box carries the
    number of members it would actually reach, because "12 of 143" is the fact that stops a club
    believing a push was the whole announcement.

    Mailchimp and Brevo are the one pair that are mutually exclusive rather than merely independent
    (see clean): they are two boxes because they are two accounts, not because a club has two
    different sets of people.
    """

    class Meta:
        model = ClubAnnouncement
        fields = [
            "text",
            "send_to_discord",
            "send_to_push",
            "send_to_mailchimp",
            "send_to_brevo",
            "show_on_website",
            "scheduled_for",
        ]
        widgets = {
            "text": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Bring a plant to Saturday's meeting — we're doing a swap."}
            ),
            # A native datetime-local input rather than the site's DateTimePickerInput: that widget
            # initializes on DOMContentLoaded, and the native one is the same control every phone
            # already knows. See the datepicker note in CLAUDE.md.
            "scheduled_for": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }

    def __init__(self, *args, **kwargs):
        self.club = kwargs.pop("club")
        super().__init__(*args, **kwargs)
        from auctions import announcements as announcements_module

        self.fields["text"].label = "Announcement"
        # The attribute is the browser's cap; clean_text below is the one that actually holds,
        # because assigning max_length after the field is built never adds its validator.
        self.fields["text"].widget.attrs["maxlength"] = announcements_module.MAX_LENGTH

        self.discord_ready = announcements_module.discord_ready(self.club)
        self.push_reachable, self.member_total = announcements_module.member_counts(self.club)

        discord = self.fields["send_to_discord"]
        discord.label = "Discord"
        if self.discord_ready:
            discord.help_text = "The channel you set with /announcements_here."
        else:
            # A checkbox that cannot do anything is disabled here rather than left clickable: this
            # is a form field whose value would be silently dropped, not an action button, so the
            # "unavailable actions stay clickable" rule in style_reference.md doesn't apply. The
            # help text carries the fix, which is the part that matters.
            discord.disabled = True
            discord.initial = False
            if not self.club.discord_server_id:
                discord.help_text = format_html(
                    "No Discord server is connected. <a href='{}'>Connect one</a> first.",
                    reverse("club_discord_config", kwargs={"slug": self.club.slug}),
                )
            else:
                discord.help_text = "No channel set. Run /announcements_here in the one you want."

        push = self.fields["send_to_push"]
        push.label = "Push notifications"
        push.help_text = (
            f"{self.push_reachable} of your {self.member_total} member{pluralize(self.member_total)} "
            "have the app with notifications on."
        )
        if not self.push_reachable:
            push.disabled = True
            push.initial = False
            push.help_text = "Nobody in your club has the app with notifications on yet."

        website = self.fields["show_on_website"]
        website.label = "Website"
        # Nothing is ticked when the form opens, including this one -- the model default is True
        # because a row created any other way should still reach the club's page, but on this form
        # a pre-ticked box is a channel nobody chose. clean() already refuses a send with no
        # channel at all, so the cost of forgetting is an error message, not a silent publish.
        website.initial = False
        website.help_text = format_html(
            "Your club page, and the <a href='{}'>snippets</a> for your own site.",
            reverse("club_website_integration", kwargs={"slug": self.club.slug}),
        )

        self.mailchimp_ready = announcements_module.mailchimp_ready(self.club)
        self.brevo_ready = announcements_module.brevo_ready(self.club)
        # A club that has connected one provider is not shopping for the other, and a permanently
        # disabled "Connect Brevo" box next to a working Mailchimp one is a box that can only ever
        # be wrong. Offer both only while neither is connected, which is the case where the pair is
        # a menu rather than a distraction.
        mailchimp_connected = bool(self.club.mailchimp_access_token)
        brevo_connected = bool(self.club.brevo_api_key)
        if mailchimp_connected and not brevo_connected:
            del self.fields["send_to_brevo"]
        elif brevo_connected and not mailchimp_connected:
            del self.fields["send_to_mailchimp"]
        self._configure_email_channel(
            "send_to_mailchimp",
            "Mailchimp",
            ready=self.mailchimp_ready,
            connected=mailchimp_connected,
            config_urlname="club_mailchimp_config",
            list_word="audience",
        )
        self._configure_email_channel(
            "send_to_brevo",
            "Brevo",
            ready=self.brevo_ready,
            connected=brevo_connected,
            config_urlname="club_brevo_config",
            list_word="list",
        )

        scheduled = self.fields["scheduled_for"]
        scheduled.required = False
        scheduled.help_text = ""

        # No subject box at all: the emailed version is always "<Club> announcement"
        # (ClubAnnouncement.email_subject). A club given the box wrote its one-sentence
        # announcement into it a second time, and the inbox showed the same words twice.
        self.helper = FormHelper()
        self.helper.form_method = "post"
        layout_fields = ["text"]
        layout_fields += [
            name
            for name in (
                "send_to_discord",
                "send_to_push",
                "send_to_mailchimp",
                "send_to_brevo",
                "show_on_website",
                "scheduled_for",
            )
            if name in self.fields
        ]
        self.helper.layout = Layout(*layout_fields)
        self.helper.add_input(Submit("submit", "Send announcement", css_class="btn-success text-dark"))

    def _configure_email_channel(self, field_name, provider, *, ready, connected, config_urlname, list_word):
        """Offer one email provider honestly: what it would reach, or why it can't.

        Same shape as the Discord checkbox above — a box that cannot do anything is disabled with
        the fix in its help text, because a form field whose value gets silently dropped is not the
        "unavailable actions stay clickable" case.
        """
        field = self.fields.get(field_name)
        if field is None:
            # The club has the other provider connected, so this one was dropped above.
            return
        field.label = provider
        if ready:
            field.help_text = ""
            return
        field.disabled = True
        field.initial = False
        if connected:
            field.help_text = format_html(
                "{} is connected but no {} is chosen. <a href='{}'>Pick one</a> first.",
                provider,
                list_word,
                reverse(config_urlname, kwargs={"slug": self.club.slug}),
            )
        else:
            field.help_text = format_html(
                "<a href='{}'>Connect {}</a> to email your members.",
                reverse(config_urlname, kwargs={"slug": self.club.slug}),
                provider,
            )

    def clean_text(self):
        """Cap the length here rather than on the model.

        Discord refuses a message over 2000 characters outright and a phone's lock screen shows
        maybe two lines, so a long announcement is not a long announcement -- it is one that
        arrives truncated in three different places, each cut somewhere different.
        """
        from auctions import announcements as announcements_module

        text = (self.cleaned_data.get("text") or "").strip()
        if len(text) > announcements_module.MAX_LENGTH:
            msg = (
                f"That's {len(text)} characters. Keep an announcement under "
                f"{announcements_module.MAX_LENGTH} — Discord and a phone's lock screen will both "
                "cut it off, in different places."
            )
            raise forms.ValidationError(msg)
        return text

    def clean_scheduled_for(self):
        """A time in the past is somebody meaning "now", or getting the date wrong. Neither is safe.

        Sending it immediately would surprise them; storing it would have the beat send it on its
        next tick, which is the same surprise a few minutes later. A grace minute covers the clock
        skew between the phone that filled the box in and this server.
        """
        when = self.cleaned_data.get("scheduled_for")
        if when and when < timezone.now() - datetime.timedelta(minutes=1):
            msg = "That time has already passed. Pick a time in the future, or leave it blank to send now."
            raise forms.ValidationError(msg)
        return when

    def clean(self):
        cleaned_data = super().clean()
        if not any(
            (
                cleaned_data.get("send_to_discord"),
                cleaned_data.get("send_to_push"),
                cleaned_data.get("send_to_mailchimp"),
                cleaned_data.get("send_to_brevo"),
                cleaned_data.get("show_on_website"),
            )
        ):
            # An announcement with no channel is a diary entry. Refuse it here rather than saving a
            # row that reaches nobody and leaves the admin thinking they told their club something.
            msg = "Pick at least one place to send this."
            raise forms.ValidationError(msg)
        if cleaned_data.get("send_to_mailchimp") and cleaned_data.get("send_to_brevo"):
            # Members are synced to every connected provider, so both lists hold the same people
            # and both campaigns would land in the same inboxes. A club keeps two providers
            # connected while it moves between them; that is a reason to have both configured, not
            # a reason to send to both at once.
            msg = (
                "Pick one email provider, not both — your members are on both lists, so sending "
                "through both puts two copies of this in the same inbox."
            )
            raise forms.ValidationError(msg)
        return cleaned_data


class ClubEditForm(forms.ModelForm):
    """Form for club admins to edit their club settings."""

    class Meta:
        model = Club
        fields = [
            "name",
            "icon",
            "homepage",
            "facebook_page",
            "discord_invite_link",
            "allow_joining",
            "enable_breeder_award_program",
            "description",
            "location",
            "location_coordinates",
        ]
        help_texts = {
            "name": "Changing this will change the URL for your club's page, as well as any API keys you're using.",
            "allow_joining": "Let members self-join via the public club page.",
            "enable_breeder_award_program": "Track when users breed fish and show a leaderboard of top breeders.",
        }
        widgets = {
            "homepage": forms.URLInput(attrs={"placeholder": "https://www.yourclub.org"}),
            "facebook_page": forms.URLInput(attrs={"placeholder": "https://www.facebook.com/groups/yourclub"}),
            "discord_invite_link": forms.URLInput(attrs={"placeholder": "https://discord.gg/yourclub"}),
            "description": SummernoteWidget(attrs={"summernote": {"width": "100%", "height": "300px"}}),
            "location": forms.TextInput(attrs={"placeholder": "Search for your club's location"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        # Required so the icon ImageField actually uploads.
        self.helper.attrs = {"enctype": "multipart/form-data"}
        self.helper.layout = Layout(
            "name",
            "icon",
            "homepage",
            "facebook_page",
            "discord_invite_link",
            Div(
                Div("allow_joining", css_class="col-6"),
                Div("enable_breeder_award_program", css_class="col-6"),
                css_class="row",
            ),
            "description",
            Fieldset(
                "Location",
                "location",
                "location_coordinates",
            ),
        )
        self.helper.add_input(Submit("submit", "Save settings", css_class="btn-primary"))


class LotCategoryForm(forms.ModelForm):
    """The BAP admin's "set category" modal.  One field, and one side effect.

    The side effect is :func:`note_category_chosen_by_person`: this form exists so a person can
    overrule where a lot landed, and without clearing ``category_automatically_added`` the next
    save would re-derive the category from the lot's species and put it straight back.
    """

    class Meta:
        model = Lot
        fields = ["species_category"]

    def clean(self):
        cleaned_data = super().clean()
        note_category_chosen_by_person(self.instance, cleaned_data)
        return cleaned_data

    def __init__(self, *args, **kwargs):
        post_url = kwargs.pop("post_url", None)
        if not post_url:
            msg = "LotCategoryForm requires a post_url."
            raise ValueError(msg)
        super().__init__(*args, **kwargs)
        self.fields["species_category"].queryset = Category.objects.all().order_by("name")
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.attrs = {"hx-post": post_url, "hx-target": "#modals-here", "hx-swap": "innerHTML"}
        self.helper.layout = Layout(
            "species_category",
            Div(
                HTML('<button type="submit" class="btn btn-primary">Save</button>'),
                HTML('<button type="button" class="btn btn-secondary" onclick="closeModal()">Cancel</button>'),
                css_class="d-flex gap-2",
            ),
        )


class _ClubEmailMemberChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        email = obj.routing_email
        if obj.name and email:
            return f"{obj.name} <{email}>"
        return email or str(obj)


class ClubMembershipSettingsForm(forms.ModelForm):
    """Form for club admins to configure membership and payment settings.

    The PayPal/Square seller for the club is managed separately (via the seller info
    cards rendered on the same template), not as a field on this form.
    """

    class Meta:
        model = Club
        fields = [
            "membership_system",
            "membership_annual_fee",
            "show_member_barcode",
            "paypal_webhook_id",
        ]
        help_texts = {
            "membership_system": (
                "No membership fees: members never pay dues and expiration tracking is off. "
                "January 1st: all memberships expire on Jan 1 each year. "
                "Rolling: memberships expire one year from the payment date."
            ),
            "membership_annual_fee": "The amount members pay each year to renew. Required when dues are enabled.",
        }

    def __init__(self, *args, show_paypal_subscriptions=True, **kwargs):
        kwargs.pop("current_user", None)
        # The full, copyable URL to paste into PayPal; falls back to the path if the view didn't
        # supply it (e.g. in a unit test that builds the form directly).
        webhook_url = kwargs.pop("webhook_url", "") or "/clubs/paypal/webhook"
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"

        layout_fields = ["membership_system", "membership_annual_fee", "show_member_barcode"]
        if show_paypal_subscriptions:
            self.fields["paypal_webhook_id"].label = "PayPal webhook ID"
            self.fields["paypal_webhook_id"].required = False
            setup_toggle_html = (
                '<button class="btn btn-primary btn-sm mb-2" type="button" '
                'data-bs-toggle="collapse" data-bs-target="#paypalSubSetup" aria-expanded="false" '
                'aria-controls="paypalSubSetup"><i class="bi bi-paypal"></i> '
                "PayPal membership subscriptions &mdash; setup instructions</button>"
            )
            setup_instructions_html = (
                '<p class="small text-muted mb-2">Let members pay dues with a recurring PayPal subscription '
                "so their membership renews automatically. Set this up once in the PayPal account that "
                "receives your dues:</p>"
                '<ol class="small text-muted mb-2 ps-3">'
                "<li>Create a subscription plan and share its link with members under "
                '<a href="https://www.paypal.com/billing/plans" target="_blank" rel="noopener">'
                "PayPal &rarr; Pay &amp; Get Paid &rarr; Subscriptions</a>. Members subscribe from that "
                "link &mdash; that is what starts the automatic renewals.</li>"
                '<li>In the <a href="https://developer.paypal.com/dashboard/applications/live" '
                'target="_blank" rel="noopener">PayPal Developer Dashboard &rarr; Apps &amp; Credentials'
                "</a>, open the app for that account and add a webhook pointing to the URL below.</li>"
                "<li>Subscribe that webhook to these events: <code>BILLING.SUBSCRIPTION.ACTIVATED</code>, "
                "<code>CANCELLED</code>, <code>SUSPENDED</code>, <code>EXPIRED</code>, "
                "<code>UPDATED</code>, and <code>PAYMENT.SALE.COMPLETED</code> (that last one is how "
                "each recurring renewal payment reaches us).</li>"
                "<li>Copy the <strong>Webhook ID</strong> PayPal shows you and paste it in the box "
                "below so we can verify and process your members&rsquo; subscription payments.</li>"
                "</ol>"
                '<div class="input-group input-group-sm mb-2">'
                f'<input type="text" class="form-control" id="paypalWebhookUrl" readonly value="{webhook_url}" '
                'onclick="this.select()">'
                '<button class="btn btn-primary" type="button" '
                "onclick=\"var i=document.getElementById('paypalWebhookUrl');i.select();"
                'navigator.clipboard&amp;&amp;navigator.clipboard.writeText(i.value);">Copy</button>'
                "</div>"
            )
            layout_fields += [
                HTML(setup_toggle_html),
                Div(
                    HTML(setup_instructions_html),
                    "paypal_webhook_id",
                    css_class="collapse border rounded p-3 mb-3",
                    css_id="paypalSubSetup",
                ),
            ]
        else:
            # Subscriptions can only be verified for site-PayPal or own-credential clubs (see
            # Club.supports_paypal_subscriptions). Drop the field entirely for everyone else so it
            # isn't rendered -- and, since it's no longer part of the form, a previously saved value
            # is preserved rather than blanked on submit.
            self.fields.pop("paypal_webhook_id", None)

        self.helper.layout = Layout(*layout_fields)
        self.helper.add_input(Submit("submit", "Save membership settings", css_class="btn-primary"))

    def clean(self):
        cleaned_data = super().clean()
        membership_system = cleaned_data.get("membership_system")
        fee = cleaned_data.get("membership_annual_fee")
        if membership_system == "none":
            # "No membership fees" always means a zero fee, regardless of what was submitted.
            cleaned_data["membership_annual_fee"] = Decimal(0)
        elif not fee or fee <= 0:
            # A paid membership system requires a real fee — a zero fee would silently
            # disable dues, payment buttons, and reminders while still showing as "paid".
            self.add_error(
                "membership_annual_fee",
                'Enter a fee greater than 0, or choose "No membership fees" above.',
            )
        return cleaned_data


class ClubPayPalCredentialsForm(forms.ModelForm):
    """Lets a club using non-OAuth PayPal enter its own REST API credentials.

    Only used when ``Club.allow_non_oauth_paypal`` is set (an admin-only flag); the
    membership settings template renders this in place of the PayPal OAuth UI. The secret
    is write-only -- it is never rendered back, and leaving it blank keeps the saved value.
    """

    paypal_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        label="PayPal secret",
        help_text="Leave blank to keep the saved secret. Stored encrypted.",
    )

    class Meta:
        model = Club
        fields = ["paypal_client_id", "paypal_secret"]
        help_texts = {
            "paypal_client_id": "REST API client ID from your club's own PayPal app.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Stop Firefox/Chrome from offering the admin's own login here: the client ID/secret
        # pair otherwise looks like a username/password login form. "new-password" is the only
        # value browsers reliably honor on a password field ("off" is ignored for logins).
        self.fields["paypal_client_id"].widget.attrs["autocomplete"] = "off"
        self.fields["paypal_secret"].widget.attrs["autocomplete"] = "new-password"
        self.helper = FormHelper()
        # The template renders the <form> tag itself so it can point at the dedicated endpoint.
        self.helper.form_tag = False
        self.helper.layout = Layout("paypal_client_id", "paypal_secret")
        self.helper.add_input(Submit("submit", "Save PayPal credentials", css_class="btn-primary"))

    def clean_paypal_secret(self):
        # Blank means "keep what's stored" so the saved secret is never echoed back to the page.
        secret = self.cleaned_data.get("paypal_secret")
        if not secret:
            return self.instance.paypal_secret
        return secret


class ClubEmailSettingsForm(forms.ModelForm):
    class Meta:
        model = Club
        fields = [
            "auction_email_member",
            "contact_email_member",
            "donation_email_member",
            "contact_email",
            "send_welcome_email_to_new_members",
            "send_membership_expiration_reminders_30_days",
            "send_membership_expiration_reminders",
            "send_membership_renewal_confirmation",
            "welcome_opening",
            "welcome_closing",
            "welcome_include_auction",
            "renewal_opening",
            "renewal_closing",
            "renewal_include_auction",
            "expiring_soon_opening",
            "expiring_soon_closing",
            "expiring_soon_include_auction",
        ]

    def __init__(self, *args, **kwargs):
        show_email_routing = kwargs.pop("show_email_routing", True)
        super().__init__(*args, **kwargs)
        club = self.instance
        self.helper = FormHelper()
        self.helper.form_method = "post"
        # The template renders the <form> tag itself so it can wrap both the
        # crispy fields and the email-preview mockup (which embeds the welcome
        # text textarea inline).
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        payments_enabled = bool(club and club.membership_payment_emails_enabled)
        layout_fields = []
        if show_email_routing:
            layout_fields.append(
                Fieldset(
                    "Incoming email routing",
                    "auction_email_member",
                    "contact_email_member",
                    "donation_email_member",
                )
            )
        else:
            # When SES routing is off there is no per-member routing; expose the
            # plain reply-to address here instead of on the membership settings page.
            layout_fields.append(
                Fieldset(
                    "Reply-to address",
                    "contact_email",
                )
            )
        # The four toggles are rendered by crispy. The email opening/closing fields
        # and include_auction checkboxes are rendered manually in the template
        # (inside the preview mockup) — they are excluded from the layout here
        # but still in Meta.fields so they post.
        outgoing_fields = [
            "send_welcome_email_to_new_members",
            "send_membership_expiration_reminders_30_days",
            "send_membership_expiration_reminders",
            "send_membership_renewal_confirmation",
        ]
        layout_fields.append(Fieldset("Outgoing emails", *outgoing_fields))
        self.helper.layout = Layout(*layout_fields)
        if show_email_routing:
            # The contact_email field is hidden when SES routing is enabled — it is
            # not exposed in the layout and members route via contact_email_member.
            self.fields.pop("contact_email", None)
        else:
            # Drop the dropdown routing fields entirely so they are not posted.
            self.fields.pop("auction_email_member", None)
            self.fields.pop("contact_email_member", None)
            self.fields.pop("donation_email_member", None)
            if "contact_email" in self.fields:
                self.fields["contact_email"].label = "Membership email address"
                self.fields[
                    "contact_email"
                ].help_text = "Replies to outgoing membership emails will be sent to this address."
        if club and club.pk and show_email_routing:
            base_qs = (
                club.members.filter(is_deleted=False)
                .filter((Q(email__isnull=False) & ~Q(email="")) | (Q(user__email__isnull=False) & ~Q(user__email="")))
                .order_by("name", "email")
            )
            auction_qs = base_qs.filter(Q(permission_admin=True) | Q(permission_manage_auctions=True))
            contact_qs = base_qs.filter(Q(permission_admin=True) | Q(permission_add_edit=True))
            # Donation replies go to whoever may open the vendor pages, which is its own permission
            # now -- offering a membership manager here would name a recipient that
            # Club.donation_email_recipient then refuses, and the setting would look silently broken.
            donation_qs = base_qs.filter(Q(permission_admin=True) | Q(permission_manage_donations=True))
            # Determine the fallback person shown in the help text
            auction_fallback = club._first_email_member_by_priority(Q(permission_manage_auctions=True))
            contact_fallback = club._first_email_member_by_priority(Q(permission_add_edit=True))
        else:
            auction_qs = ClubMember.objects.none()
            contact_qs = ClubMember.objects.none()
            donation_qs = ClubMember.objects.none()
            auction_fallback = None
            contact_fallback = None

        def _fallback_label(member):
            if not member:
                return ""
            name = member.name or member.routing_email
            email = member.routing_email
            if name and email and name != email:
                return f" ({name} <{email}>)"
            if email:
                return f" ({email})"
            return ""

        if show_email_routing:
            self.fields["auction_email_member"] = _ClubEmailMemberChoiceField(
                queryset=auction_qs,
                required=False,
                label="Auction replies",
                help_text=(
                    f"Replies sent to {club.auction_sender_email or 'club-slug-auctions@your-domain'} are routed to this member. "
                    f"Leave blank to fall back to the first club admin or auction manager with an email address{_fallback_label(auction_fallback)}."
                ),
            )
            self.fields["contact_email_member"] = _ClubEmailMemberChoiceField(
                queryset=contact_qs,
                required=False,
                label="Contact replies",
                help_text=(
                    f"Replies sent to {club.contact_sender_email or 'club-slug-contact@your-domain'} are routed to this member. "
                    f"Leave blank to fall back to the first club admin or membership manager with an email address{_fallback_label(contact_fallback)}."
                ),
            )
            donation_enabled = bool(club and club.pk and club.enable_donation_tracking)
            if donation_enabled:
                donation_help = (
                    "Vendor replies are always recorded against the vendor. Leave this blank "
                    "(recommended) so they are recorded and nothing else — forwarding them to a "
                    "person invites a reply from that person's own inbox, which this site never "
                    "sees and can't track. Set it only if someone needs a copy in their inbox."
                )
            else:
                donation_help = "Turn on donation tracking in Setup to route donation replies."
            self.fields["donation_email_member"] = _ClubEmailMemberChoiceField(
                queryset=donation_qs if donation_enabled else ClubMember.objects.none(),
                required=False,
                label="Donation replies",
                help_text=donation_help,
                disabled=not donation_enabled,
            )
        self.fields["send_welcome_email_to_new_members"].label = "Send welcome letter to new club members"
        self.fields[
            "send_membership_expiration_reminders_30_days"
        ].label = "Send expiration reminder 30 days before membership expires"
        self.fields[
            "send_membership_expiration_reminders"
        ].label = "Send expiration reminder the day before membership expires"
        self.fields["send_membership_renewal_confirmation"].label = "Send membership renewal confirmation"
        reminder_help = (
            "Requires integrated membership payments so the email can link members back to their renewal page."
        )
        self.fields["send_membership_expiration_reminders_30_days"].help_text = reminder_help
        self.fields["send_membership_expiration_reminders"].help_text = reminder_help
        if not payments_enabled:
            self.fields["send_membership_expiration_reminders_30_days"].disabled = True
            self.fields["send_membership_expiration_reminders"].disabled = True
            self.fields[
                "send_membership_expiration_reminders_30_days"
            ].help_text = "No connected payment account, expiration emails will not be sent"
            self.fields[
                "send_membership_expiration_reminders"
            ].help_text = "No connected payment account, expiration emails will not be sent"
            self.fields["expiring_soon_include_auction"].disabled = True

    _EMAIL_TEXT_FIELDS = [
        "welcome_opening",
        "welcome_closing",
        "renewal_opening",
        "renewal_closing",
        "expiring_soon_opening",
        "expiring_soon_closing",
    ]
    # ``<`` is excluded as well as ``>`` so an unterminated "<" stops at the next one instead of
    # scanning to the end of the value from every "<" in it, which is quadratic (see the same fix in
    # auctions/donations.py). A real tag never contains a bare "<", so nothing this rejected before
    # gets through now.
    _HTML_TAG_RE = re.compile(r"<[^<>]+>")
    _URL_RE = re.compile(r"https?://", re.IGNORECASE)

    def clean(self):
        cleaned = super().clean()
        for field_name in self._EMAIL_TEXT_FIELDS:
            value = cleaned.get(field_name, "") or ""
            if self._HTML_TAG_RE.search(value):
                self.add_error(field_name, "HTML tags are not allowed in email text.")
            elif self._URL_RE.search(value):
                self.add_error(field_name, "Links (URLs) are not allowed in email text.")
        return cleaned


class ClubBapSettingsForm(forms.ModelForm):
    """Form for BAP admins to configure Breeder Award Program settings for a club."""

    class Meta:
        model = Club
        fields = [
            "auto_add_points",
            "points_per_lot",
            "points_for_custom_checkbox",
            "min_quantity",
            "days_between_same_name_lots",
            "days_between_same_species_lots",
            "only_active_members_can_participate",
            "only_donation_lots",
            "only_sold_lots",
            "no_min_bids",
            "separate_hap",
            "separate_cap",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.layout = Layout(
            Fieldset(
                "Point rules",
                "auto_add_points",
                "points_per_lot",
                "points_for_custom_checkbox",
                "min_quantity",
                "days_between_same_name_lots",
                "days_between_same_species_lots",
                "only_active_members_can_participate",
                "only_donation_lots",
                "only_sold_lots",
                "no_min_bids",
                "separate_hap",
                "separate_cap",
            ),
        )
        self.helper.add_input(Submit("submit", "Save BAP settings", css_class="btn-primary"))


class ClubBapCategoryOverrideForm(forms.ModelForm):
    """Inline form for adding/updating a per-category BAP point override for a club."""

    category = forms.ModelChoiceField(
        queryset=Category.objects.all().order_by("name"),
        widget=autocomplete.ModelSelect2(url="category-autocomplete"),
        label="Category",
    )

    class Meta:
        model = ClubBapCategoryOverride
        fields = ["category", "points"]
        widgets = {
            "points": forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "width:6rem"}),
        }


class SpeciesAdminForm(forms.ModelForm):
    """Add a species -- or a strain of one -- from the site, without opening the Django admin.

    The species list is imported, not typed, and that is the point: 36,000 rows nobody has to
    maintain.  But the imported list will always be missing things a club sells -- an undescribed
    *Ancistrus* with an L-number, this year's shrimp colour, a plant the trade renamed -- and the
    lot form deliberately refuses to accept a name that isn't on the list.  So there has to be a
    way to *add to the list*, and it has to be quick enough to use while somebody is standing at
    the check-in table.

    Two things make it quick.  The scientific name is one box, split on the space rather than
    asked for twice.  And a strain is the same form with a parent picked, which is what keeps
    "Blue Dream" out of the genus column -- see :class:`~auctions.models.Species`.

    A **hybrid** is the third shape and the only one with no scientific name at all: tick the box,
    name the cross, and leave the rest.  It is a checkbox rather than "a strain with no parent"
    because a strain with no parent is the commonest way to fill this form in wrong, and the two
    have to be told apart by something the person actually said.

    Everything created here is ``source="admin"``, which is *not* the same as the ``manual`` rows
    left over from the old Product table: those get folded into the imported list by
    ``import_fishbase``, and a row somebody added on purpose last week must not be.
    """

    scientific_name_input = forms.CharField(
        label="Scientific name",
        max_length=250,
        required=False,
        help_text="Genus and species, e.g. <i>Ancistrus cirrhosus</i>. A genus on its own is fine.",
    )
    other_names = forms.CharField(
        label="Other common names",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="One per line, or separated by commas. These are what people actually type into a lot name.",
    )
    lot_name = forms.CharField(widget=forms.HiddenInput(), required=False)
    attach_to_lots = forms.BooleanField(
        required=False,
        initial=True,
        label="Set this species on the lots with that name, and remember the name",
    )

    class Meta:
        model = Species
        fields = [
            "common_name",
            "is_hybrid",
            "variety",
            "parent",
            "category",
            "freshwater",
            "brackish",
            "saltwater",
            "breeder_points",
        ]

    def __init__(self, *args, lot_name="", lot_count=0, added_by=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lot_count = lot_count
        self.added_by = added_by
        self.fields["common_name"].label = "Common name"
        self.fields["common_name"].help_text = "What the picker shows in brackets, e.g. Bristlenose pleco."
        self.fields["variety"].label = "Strain or hybrid name"
        self.fields["variety"].help_text = (
            "For a strain or cultivar, e.g. Blue Dream — pick the species it is a strain of below. "
            "For a hybrid, the name the trade uses, e.g. Tibee."
        )
        self.fields["is_hybrid"].label = "This is a hybrid"
        self.fields["is_hybrid"].help_text = (
            "A cross with no accepted scientific name — a tibee shrimp, a flowerhorn. Leave the "
            "scientific name blank and name the cross above; it shows as <i>Hybrid 'Tibee'</i>, so a "
            "judge reading the label can see what it is."
        )
        self.fields["parent"].label = "Strain of"
        self.fields["parent"].required = False
        self.fields["parent"].widget = autocomplete.ModelSelect2(
            url="species-autocomplete",
            attrs={"data-placeholder": "Search the species list…", "style": "width: 100%"},
        )
        # Re-assigning the queryset is what rebinds widget.choices to it.  Without this the
        # autocomplete widget is left holding the plain list Django built for the old widget, and
        # re-rendering the form -- which only happens when there is a validation error to show --
        # dies inside dal trying to filter it.
        # Nominal species only, and no hybrids: a strain of a strain is not a thing, and a strain
        # of a cross would inherit a genus the cross deliberately hasn't got.
        self.fields["parent"].queryset = visible_species(added_by).filter(parent__isnull=True, is_hybrid=False)
        self.fields["parent"].help_text = (
            "Leave blank for an ordinary species. A strain keeps its parent's genus and epithet, so "
            "breeder points and BAP genus rules still see the plain species."
        )
        self.fields["category"].widget = autocomplete.ModelSelect2(
            url="category-autocomplete", attrs={"data-placeholder": "Search categories…", "style": "width: 100%"}
        )
        self.fields["category"].queryset = Category.objects.all().order_by("name")
        self.fields["category"].help_text = "Lots with this species are filed here automatically."
        self.fields["freshwater"].initial = True
        if lot_name:
            self.fields["lot_name"].initial = lot_name
            self.fields["attach_to_lots"].label = (
                f"Also set this species on the {lot_count} lot{'' if lot_count == 1 else 's'} "
                f"called \u201c{lot_name}\u201d, and remember the name for next time"
            )
        else:
            self.fields["attach_to_lots"].widget = HiddenInput()
        add_bootstrap_classes(self)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.layout = Layout(
            Div(
                Div("scientific_name_input", css_class="col-md-6"),
                Div("common_name", css_class="col-md-6"),
                Div("parent", css_class="col-md-6"),
                Div("variety", css_class="col-md-6"),
                Div("is_hybrid", css_class="col-md-12"),
                Div("other_names", css_class="col-md-12"),
                Div("category", css_class="col-md-6"),
                Div("breeder_points", css_class="col-md-6"),
                Div("freshwater", css_class="col-md-2"),
                Div("brackish", css_class="col-md-2"),
                Div("saltwater", css_class="col-md-2"),
                Div("attach_to_lots", css_class="col-md-12 mt-2"),
                "lot_name",
                css_class="row",
            ),
            Submit("submit", "Add species", css_class="btn-success mt-2"),
        )

    def clean(self):
        cleaned_data = super().clean()
        typed = (cleaned_data.get("scientific_name_input") or "").strip()
        parent = cleaned_data.get("parent")
        variety = (cleaned_data.get("variety") or "").strip()
        is_hybrid = bool(cleaned_data.get("is_hybrid"))
        if is_hybrid:
            # The whole of a cross's identity is the name the trade gave it.  Anything else on the
            # form is a contradiction rather than extra detail, so it is an error and not a field
            # quietly thrown away on save.
            if parent:
                self.add_error("parent", "A hybrid is not a strain of one species — that is what makes it a hybrid.")
            if typed:
                self.add_error(
                    "scientific_name_input", "Leave this blank for a hybrid: a cross has no scientific name."
                )
            if not variety:
                self.add_error("variety", "Give the hybrid the name the trade uses for it, e.g. Tibee.")
                return cleaned_data
            cleaned_data["genus"] = ""
            cleaned_data["species"] = ""
        else:
            if variety and not parent:
                self.add_error(
                    "parent",
                    "A strain has to say which species it is a strain of.  "
                    "Tick “this is a hybrid” if it is a cross with no species of its own.",
                )
            if parent and not variety:
                self.add_error("variety", "Give the strain a name, e.g. Blue Dream.")
            # A strain takes its parent's name; there is nothing to type and nothing to disagree about.
            if parent:
                cleaned_data["genus"] = parent.genus
                cleaned_data["species"] = parent.species
            elif typed:
                cleaned_data["genus"], cleaned_data["species"] = split_scientific_name(typed)
            else:
                self.add_error(
                    "scientific_name_input", "Enter a scientific name, or pick a species to add a strain to."
                )
                return cleaned_data
        clash = species_already_named(
            cleaned_data["genus"], cleaned_data["species"], variety, user=self.added_by, is_hybrid=is_hybrid
        )
        if clash:
            # Not an error to fix by editing the name -- the answer is to go and use the row that
            # already exists, so say where it is.
            if self.added_by and self.added_by.is_superuser:
                message = mark_safe(  # noqa: S308 - the only interpolation is a URL we build and an escaped name
                    f"{escape(clash.label)} is already on the list. "
                    f'<a href="/admin/auctions/species/{clash.pk}/change/">Edit it</a> instead.'
                )
            else:
                message = f"{clash.label} is already on the list — search for it on the lot form instead."
            self.add_error("scientific_name_input", message)
        return cleaned_data

    def save(self, commit=True):
        species = super().save(commit=False)
        species.genus = self.cleaned_data["genus"]
        species.species = self.cleaned_data["species"]
        # A cross with the common-name box left empty would have no name a person could type: the
        # picker shows "Hybrid 'Tibee'" and every lookup reads the name table, not the variety
        # column.  The trade name is the answer to both.
        if species.is_hybrid and not species.common_name:
            species.common_name = species.variety[:255]
        species.source = "admin"
        # Somebody is adding this because a club is selling one, which is better evidence than
        # FishBase's column -- see Species.in_aquarium_trade.
        species.in_trade_override = True
        species.added_by = self.added_by
        # Filled in when there is an obvious club and left blank otherwise, which is most of the
        # reason the visibility rule is "user *or* club": a species with no club is still visible
        # to whoever added it.  See UserData.only_club and species_matching.visible_species.
        species.club = self.added_by.userdata.only_club if self.added_by else None
        # A superuser is adding to everybody's list and knows it.  An auction admin is solving a
        # problem in front of them, which is a different and much narrower claim -- so their row
        # is theirs until somebody approves it.  See Species.approved and visible_species().
        species.approved = bool(self.added_by and self.added_by.is_superuser)
        if commit:
            species.save()
            names = re.split(r"[,\n]+", self.cleaned_data.get("other_names") or "")
            # A hybrid's strain name is the only name it has, and the matcher reads names out of
            # SpeciesCommonName -- nothing looks at the variety column.  So it goes in the list
            # too, or a cross would be on the picker and unreachable by typing what it is called.
            wanted = [species.common_name] + [name.strip() for name in names]
            if species.is_hybrid and species.variety:
                wanted.append(species.variety)
            seen = set()
            for index, name in enumerate(wanted):
                key = (name or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                SpeciesCommonName.objects.create(
                    species=species,
                    name=name.strip()[:255],
                    language="English",
                    is_preferred=(index == 0),
                    source="admin",
                    # Stamped like the species itself: these names arrived with it, and they
                    # become everybody's at the same moment it does.  See SpeciesApproveView.
                    approved=species.approved,
                    added_by=species.added_by,
                    club=species.club,
                )
            Species.recompute_trade_ranks(genus=species.genus)
        return species


class SpeciesCommonNameForm(forms.Form):
    """Teach the site a name for a species that is **already** on the list.

    The other half of :class:`SpeciesAdminForm`, and the commoner of the two jobs.  Most lot names
    with no scientific name are not a missing species at all -- they are a species the list has
    had all along, under a name nobody in the hobby uses.  FishBase files *Labidochromis
    caeruleus* under "Blue streak hap"; the answer to "yellow lab" matching nothing is a name, and
    adding a second *Labidochromis caeruleus* to get one is how the duplicate table fills up.

    Until this existed the only way to add a name was the Django admin, which auction admins
    cannot open -- so the workflow they were left with was the one that makes duplicates.

    Everything written here is scoped exactly like a species, and it has to be: a common name is
    read *ahead* of everything else the matcher does, so an unscoped one would let one club teach
    every other club a name for the wrong fish.  See
    :func:`~auctions.species_matching.visible_common_names`.
    """

    species = forms.ModelChoiceField(
        queryset=Species.objects.none(),
        label="Species",
        help_text="Search by scientific name, or by a name it already answers to.",
    )
    names = forms.CharField(
        label="Names people type",
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="One per line, or separated by commas. Lower case is fine; punctuation is ignored.",
    )
    lot_name = forms.CharField(widget=forms.HiddenInput(), required=False)
    attach_to_lots = forms.BooleanField(
        required=False,
        initial=True,
        label="Set this species on the lots with that name",
    )

    def __init__(self, *args, lot_name="", lot_count=0, added_by=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.added_by = added_by
        self.fields["species"].widget = autocomplete.ModelSelect2(
            url=f"{reverse('species-autocomplete')}?varieties=1",
            attrs={"data-placeholder": "Search the species list…", "style": "width: 100%"},
        )
        # After the widget, not before: re-assigning the queryset is what rebinds widget.choices to
        # it, and a dal widget left holding the plain list Django built for the old widget dies
        # inside filter_choices_to_render the moment the form is rendered.  Same trap as
        # SpeciesAdminForm's "strain of" field.
        #
        # The strains and the hybrids too: "blue dream" and "tibee" are exactly the kind of name
        # this page exists for, and both of those live on a variety row.
        self.fields["species"].queryset = visible_species(added_by)
        if lot_name:
            self.fields["lot_name"].initial = lot_name
            self.fields["names"].initial = lot_name
            self.fields["attach_to_lots"].label = (
                f"Also set this species on the {lot_count} lot{'' if lot_count == 1 else 's'} "
                f"called \u201c{lot_name}\u201d"
            )
        else:
            self.fields["attach_to_lots"].widget = HiddenInput()
        add_bootstrap_classes(self)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.layout = Layout(
            Div(
                Div("species", css_class="col-md-12"),
                Div("names", css_class="col-md-12"),
                Div("attach_to_lots", css_class="col-md-12 mt-2"),
                "lot_name",
                css_class="row",
            ),
            Submit("submit", "Add name", css_class="btn-success mt-2"),
        )

    def clean(self):
        cleaned_data = super().clean()
        species = cleaned_data.get("species")
        wanted = []
        seen = set()
        for name in re.split(r"[,\n]+", cleaned_data.get("names") or ""):
            name = name.strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            wanted.append(name)
        if not wanted:
            self.add_error("names", "Enter at least one name.")
            return cleaned_data
        if species is None:
            return cleaned_data
        for name in wanted:
            # One name on two species is the loss of a name rather than the gain of one -- the
            # matcher answers on it before anything else runs, so a shared name turns a lookup
            # that used to be unambiguous into a picklist.  Same rule as the club API.
            clash = species_carrying_common_name(name, user=self.added_by, exclude=species)
            if clash:
                self.add_error(
                    "names",
                    f"\u201c{name}\u201d is already the name for {clash.label}. "
                    "Two species with one name means neither of them can be found by it.",
                )
        cleaned_data["names"] = wanted
        return cleaned_data

    def save(self):
        """Create the names, and return the ones that were really new."""
        species = self.cleaned_data["species"]
        # A name added to a species that is already shared has no species approval to ride on, so
        # it needs one of its own.  A superuser is adding to everybody's list and knows it; an
        # auction admin gets a name their own club is answered with until somebody approves it,
        # which is the SpeciesCommonName page in the Django admin.
        approved = bool(self.added_by and self.added_by.is_superuser)
        created = []
        for name in self.cleaned_data["names"]:
            row, was_created = SpeciesCommonName.objects.get_or_create(
                species=species,
                name_normalized=normalize_species_name(name),
                defaults={
                    "name": name[:255],
                    "language": "English",
                    "source": "admin",
                    "approved": approved,
                    "added_by": self.added_by,
                    "club": self.added_by.userdata.only_club if self.added_by else None,
                },
            )
            if was_created:
                created.append(row)
        return created


class ClubBapGenusOverrideForm(forms.ModelForm):
    """Inline form for adding/updating a per-genus BAP point override for a club.

    Free text rather than a picklist: there are thousands of genera, and a club admin writing a
    BAP rule already knows the one they mean.  :meth:`clean_genus` is what keeps a typo from
    becoming a rule that silently never fires.
    """

    class Meta:
        model = ClubBapGenusOverride
        fields = ["genus", "points"]
        widgets = {
            "genus": forms.TextInput(
                attrs={"class": "form-control form-control-sm", "placeholder": "Tropheus", "list": "bap-genus-list"}
            ),
            "points": forms.NumberInput(attrs={"class": "form-control form-control-sm", "style": "width:6rem"}),
        }

    def clean_genus(self):
        genus = (self.cleaned_data.get("genus") or "").strip().capitalize()
        if not genus:
            message = "Enter a genus"
            raise forms.ValidationError(message)
        if not Species.objects.filter(genus=genus).exists():
            message = (
                f"No species in the database belong to the genus {genus}.  Check the spelling — a rule "
                "for a genus that doesn't exist would never award anything."
            )
            raise forms.ValidationError(message)
        return genus


class BapAwardForm(forms.ModelForm):
    """Form for club BAP admins to create or edit a BapAward record."""

    club_slug = forms.CharField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = BapAward
        fields = ["club_member", "date", "points", "hap_points", "cap_points", "notes"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(
        self, *args, post_url=None, delete_url=None, club=None, show_hap=False, show_cap=False, lot=None, **kwargs
    ):
        super().__init__(*args, **kwargs)
        club_slug = club.slug if club else ""
        self.fields["club_slug"].initial = club_slug
        self.fields["club_member"].widget = autocomplete.ModelSelect2(
            url="club-member-autocomplete",
            forward=["club_slug"],
            attrs={"data-placeholder": "Search for a member…", "data-html": True, "style": "width: 100%"},
        )
        if club:
            self.fields["club_member"].queryset = ClubMember.objects.filter(club=club, is_deleted=False).order_by(
                "name"
            )
        if not self.instance.pk and not self.initial.get("date"):
            self.fields["date"].initial = timezone.now().date()
        self.fields["points"].label = "BAP points"
        self.fields["hap_points"].label = "HAP points"
        self.fields["cap_points"].label = "CAP points"
        if not show_hap:
            del self.fields["hap_points"]
        if not show_cap:
            del self.fields["cap_points"]

        self.helper = FormHelper()
        self.helper.form_method = "post"
        layout_fields = list(self.fields.keys())

        prefix_items = []
        if lot:
            prefix_items.append(
                HTML(f'<p class="text-muted mb-2"><small>Lot: <strong>{lot.lot_name}</strong></small></p>')
            )
        if prefix_items:
            layout_fields = prefix_items + layout_fields
        if delete_url:
            footer = Div(
                HTML(
                    f'<button hx-post="{delete_url}" hx-target="#modals-here" hx-confirm="Delete this award?" type="button" class="btn btn-danger btn-sm">Delete</button>'
                ),
                HTML(
                    '<button type="button" class="btn btn-secondary btn-sm ms-auto" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(
                    f'<button hx-post="{post_url}" hx-target="#modals-here" hx-include="closest form" type="button" class="btn btn-primary btn-sm ms-2">Save</button>'
                ),
                css_class="modal-footer px-0 d-flex",
            )
        elif post_url:
            footer = Div(
                HTML(
                    '<button type="button" class="btn btn-secondary btn-sm" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(
                    f'<button hx-post="{post_url}" hx-target="#modals-here" hx-include="closest form" type="button" class="btn btn-primary btn-sm ms-2">Save</button>'
                ),
                css_class="modal-footer px-0",
            )
        else:
            footer = None
        self.helper.layout = Layout(*layout_fields, *([] if footer is None else [footer]))


class ClubMemberAdminForm(MarksClubMemberAdminEditedMixin, forms.ModelForm):
    """Form for club admins to edit a club member's details.

    When ``auctiontos`` is passed the form is rendered in the context of a
    specific auction.  Auction-scoped fields (pickup_location, is_club_member)
    are added; contact_status and Discord role fields are hidden because those
    are club-wide settings that don't belong in the per-auction workflow.
    """

    # Extra fields for auction context (not on ClubMember model)
    pickup_location = forms.ModelChoiceField(queryset=PickupLocation.objects.none(), required=False)
    is_club_member = forms.BooleanField(required=False, label="Alternate fees")

    class Meta:
        model = ClubMember
        fields = [
            "name",
            "memo",
            "email",
            "phone_number",
            "address",
            "contact_status",
            "send_welcome_email",
            "bidder_number",
            "bidding_allowed",
            "selling_allowed",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Name"}),
            "memo": forms.TextInput(attrs={"placeholder": "Admin notes"}),
            "email": forms.EmailInput(attrs={"placeholder": "email@example.com"}),
            "phone_number": forms.TextInput(attrs={"placeholder": "(555) 555-1234"}),
            "address": forms.Textarea(attrs={"placeholder": "123 Main St, City, State", "rows": 3}),
            "bidder_number": forms.TextInput(attrs={"placeholder": "Auto"}),
        }
        # All help texts stripped; only is_club_member retains its help text (set dynamically below)
        help_texts = dict.fromkeys(fields, "")

    def __init__(self, *args, post_url=None, read_only=False, club=None, auctiontos=None, auction=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self._club = club
        self._auctiontos = auctiontos

        # --- Auction-context extra fields ---
        # auctiontos is set when editing; auction is set when creating in check-in mode
        auction = auctiontos.auction if auctiontos else auction
        show_pickup = bool(auction and auction.multi_location)
        # Only offer the manual alternate-fees checkbox for auctions using the custom split;
        # off = never applies, club member discount = applied automatically to paid members.
        show_alt_fees = bool(auction and auction.alternate_split_mode == "custom")
        in_auction_context = bool(auctiontos or auction)

        if show_pickup:
            self.fields["pickup_location"].queryset = auction.location_qs
            self.fields["pickup_location"].initial = auctiontos.pickup_location_id if auctiontos else None
        else:
            self.fields["pickup_location"].widget = forms.HiddenInput()

        if show_alt_fees and auction:
            help_text = "Check to apply alternate fees: "
            fee_parts = []
            if auction.lot_entry_fee_for_club_members:
                fee_parts.append(f"${auction.lot_entry_fee_for_club_members} lot entry fee")
            if auction.winning_bid_percent_to_club_for_club_members:
                fee_parts.append(f"{auction.winning_bid_percent_to_club_for_club_members}% club cut")
            if fee_parts:
                help_text += ", ".join(fee_parts)
            else:
                help_text = "Check to charge no selling fees (are your rules set up correctly?)"
            label = (
                auction.alternative_split_label.capitalize() if auction.alternative_split_label else "Alternate fees"
            )
            self.fields["is_club_member"].help_text = help_text
            self.fields["is_club_member"].label = label
            if auctiontos:
                self.fields["is_club_member"].initial = auctiontos.is_club_member
        else:
            self.fields["is_club_member"].widget = forms.HiddenInput()

        # contact_status is excluded from the layout in auction context but is still a required field
        # (no blank=True). Make it a hidden input so a valid value is always submitted.
        if in_auction_context:
            self.fields["contact_status"].widget = forms.HiddenInput()
            self.fields["contact_status"].initial = (
                self.instance.contact_status if self.instance and self.instance.pk else "contact"
            )
        show_welcome_email = not (
            self.instance and self.instance.pk and (self.instance.welcome_email_sent or self.instance.source == "csv")
        )
        if not show_welcome_email or in_auction_context:
            self.fields["send_welcome_email"].widget = forms.HiddenInput()
            self.fields["send_welcome_email"].required = False
            instance_obj = self.instance if self.instance and self.instance.pk else None
            self.fields["send_welcome_email"].initial = instance_obj.send_welcome_email if instance_obj else True
        else:
            self.fields["send_welcome_email"].label = "Send welcome letter"
            self.fields["send_welcome_email"].required = False
            if not (self.instance and self.instance.pk):
                self.fields["send_welcome_email"].initial = True

        # In auction context, hide bidding_allowed/selling_allowed when the auction doesn't use them.
        # BooleanField has required=True by default; set required=False so an absent/False value
        # doesn't fail validation when the field is hidden.
        if in_auction_context and auction:
            if not auction.only_approved_sellers:
                self.fields["selling_allowed"].widget = forms.HiddenInput()
                self.fields["selling_allowed"].required = False
            if not auction.only_approved_bidders:
                self.fields["bidding_allowed"].widget = forms.HiddenInput()
                self.fields["bidding_allowed"].required = False
            # Initialise from auctiontos when editing; default to member/True for new check-ins.
            if auctiontos:
                self.fields["selling_allowed"].initial = auctiontos.selling_allowed
                self.fields["bidding_allowed"].initial = auctiontos.bidding_allowed
            else:
                instance_obj = self.instance if self.instance and self.instance.pk else None
                self.fields["selling_allowed"].initial = instance_obj.selling_allowed if instance_obj else True
                self.fields["bidding_allowed"].initial = instance_obj.bidding_allowed if instance_obj else True

        # Layout:
        #   Row: bidder_number (left) | memo (right)
        #   name
        #   Row: email | phone
        #   address (textarea)
        #   contact_status (hidden in auction context)
        #   Row: bidding_allowed | selling_allowed  (hidden when not applicable)
        #   is_club_member (alt fees, auction context only)
        #   pickup_location (multi-location auction only)
        # In auction context contact_status is hidden but still in the form — it MUST appear in the
        # layout so crispy renders it as a hidden input (crispy only auto-renders hidden fields that
        # are somewhere in the layout).
        if in_auction_context:
            contact_status_fields: list = [Field("contact_status")]
        else:
            contact_status_fields = ["contact_status"]

        bidding_selling_row = Div(
            Div("bidding_allowed", css_class="col-md-6"),
            Div("selling_allowed", css_class="col-md-6"),
            css_class="row",
        )
        alt_fees_field: list = ["is_club_member"] if show_alt_fees else []
        pickup_field: list = ["pickup_location"] if show_pickup else []

        base_fields = [
            Div(
                Div("bidder_number", css_class="col-md-4"),
                Div("memo", css_class="col-md-8"),
                css_class="row",
            ),
            "name",
            Div(
                Div("email", css_class="col-md-6"),
                Div("phone_number", css_class="col-md-6"),
                css_class="row",
            ),
            "address",
            *contact_status_fields,
            "send_welcome_email",
            bidding_selling_row,
            *alt_fees_field,
            *pickup_field,
        ]

        if read_only:
            for field in self.fields.values():
                field.disabled = True
                if hasattr(field.widget, "attrs"):
                    field.widget.attrs["style"] = "color: inherit; -webkit-text-fill-color: currentColor; opacity: 1;"
            self.helper.layout = Layout(
                *base_fields,
                Div(
                    HTML(
                        '<button type="button" class="btn btn-secondary" onmousedown="event.preventDefault()" onclick="closeModal()">Close</button>'
                    ),
                    css_class="modal-footer",
                ),
            )
        elif post_url:
            self.helper.layout = Layout(
                *base_fields,
                Div(
                    HTML(
                        '<button type="button" class="btn btn-secondary" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                    ),
                    HTML(
                        f'<button hx-post="{post_url}" hx-target="#modals-here" hx-include="closest form" type="button" class="btn btn-primary ms-2">Save</button>'
                    ),
                    css_class="modal-footer",
                ),
            )
        else:
            self.helper.layout = Layout(*base_fields)
            self.helper.add_input(Submit("submit", "Save", css_class="btn-primary"))

    def clean_bidder_number(self):
        bidder_number = (self.cleaned_data.get("bidder_number") or "").strip()
        if not bidder_number:
            return bidder_number
        club = self._club or (self.instance.club if self.instance and self.instance.pk else None)
        if not club:
            return bidder_number
        clash = (
            ClubMember.objects.filter(club=club, bidder_number=bidder_number).exclude(pk=self.instance.pk or 0).exists()
        )
        if clash:
            msg = f"Bidder number '{bidder_number}' is already used by another member in this club."
            raise forms.ValidationError(msg)
        return bidder_number


class ClubMemberDiscordForm(MarksClubMemberAdminEditedMixin, forms.ModelForm):
    """Form for managing a club member's Discord integration settings."""

    class Meta:
        model = ClubMember
        fields = ["discord_id", "discord_role_auto_managed", "discord_role_override"]
        widgets = {
            "discord_id": forms.TextInput(attrs={"placeholder": "Discord user ID (e.g. 123456789012345678)"}),
        }
        help_texts = dict.fromkeys(fields, "")

    def __init__(self, *args, post_url=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"

        instance = self.instance if self.instance and self.instance.pk else None

        # Restrict override queryset to roles the bot can manage
        if instance and instance.club:
            self.fields["discord_role_override"].queryset = instance.club.discord_roles.filter(bot_can_manage=True)
        else:
            from .models import ClubDiscordRole

            self.fields["discord_role_override"].queryset = ClubDiscordRole.objects.none()

        self.fields["discord_role_auto_managed"].required = False

        # Discord ID: readonly when set (with Clear button), editable when blank
        from django.utils.html import format_html

        has_discord_id = bool(instance and instance.discord_id)
        if has_discord_id:
            discord_id_row = HTML(
                format_html(
                    '<div class="mb-3">'
                    '<label class="form-label" for="id_discord_id">Discord ID</label>'
                    '<div class="input-group">'
                    '<input type="text" name="discord_id" id="id_discord_id" maxlength="100"'
                    ' value="{}" readonly class="form-control" autocomplete="off">'
                    '<button class="btn btn-danger" type="button" id="clear-discord-id-btn">'
                    "Clear</button>"
                    "</div></div>",
                    instance.discord_id,
                )
            )
        else:
            discord_id_row = Field("discord_id")

        discord_config_url = (
            reverse("club_discord_config", kwargs={"slug": instance.club.slug}) if instance and instance.club else "#"
        )
        help_text_html = HTML(
            f'<div class="alert alert-warning py-2 small mb-3">'
            f"You most likely do not need to change the settings here &mdash; users can connect their own Discord account using the join button. "
            f'<a href="{discord_config_url}">Click here to configure your club\'s Discord.</a>'
            f"</div>"
        )

        layout_fields = [
            help_text_html,
            discord_id_row,
            "discord_role_auto_managed",
            Field("discord_role_override", wrapper_class="discord-role-override-field"),
        ]

        if post_url:
            self.helper.layout = Layout(
                *layout_fields,
                Div(
                    HTML(
                        '<button type="button" class="btn btn-secondary" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                    ),
                    HTML(
                        f'<button hx-post="{post_url}" hx-target="#modals-here" type="submit" class="btn btn-primary ms-2">Save</button>'
                    ),
                    css_class="modal-footer",
                ),
            )
        else:
            self.helper.layout = Layout(*layout_fields)
            self.helper.add_input(Submit("submit", "Save", css_class="btn-primary"))

    def clean_discord_id(self):
        value = (self.cleaned_data.get("discord_id") or "").strip()
        return value or None

    def clean_discord_role_override(self):
        role = self.cleaned_data.get("discord_role_override")
        if role and not role.bot_can_manage:
            msg = "The bot's role is not above this role in the Discord hierarchy — it cannot be assigned to members."
            raise forms.ValidationError(msg)
        return role


class ClubMemberPermissionsForm(MarksClubMemberAdminEditedMixin, forms.ModelForm):
    """Admin-only form to set permission bool fields on a ClubMember."""

    class Meta:
        model = ClubMember
        fields = [
            "permission_admin",
            "permission_edit_club",
            "permission_money",
            "permission_manage_auctions",
            "permission_manage_bap",
            "permission_manage_donations",
            "permission_send_announcements",
            "permission_export",
            "permission_add_edit",
            "permission_view",
        ]

    def __init__(self, *args, post_url=None, **kwargs):
        super().__init__(*args, **kwargs)
        labels = {
            "permission_admin": "Club admin — can do everything, including assigning permissions to other members",
            "permission_edit_club": "Edit club settings — club setup, Discord, and API keys",
            "permission_money": "Manage membership and payments — membership/payment settings and treasurer's report",
            "permission_manage_auctions": "Manage auctions",
            "permission_manage_bap": "Award points — can manually add breeder award points to members' accounts and edit BAP settings",
            "permission_manage_donations": "Manage donations",
            "permission_send_announcements": "Send announcements — post to Discord, members' phones and the club's mailing list",
            "permission_export": "CSV import/export — can import and export member data as CSV",
            "permission_add_edit": "Manage membership — add, delete, and edit member records, renew memberships",
            "permission_view": "View members — can see the member list, but not edit",
        }
        for field_name, label in labels.items():
            self.fields[field_name].label = label
        # The donation permission is worth nothing until the club turns donation tracking on, so say
        # so where the checkbox is rather than letting an admin grant it and wonder why the person
        # they granted it to still sees no Donation Tracking link.
        club = getattr(self.instance, "club", None)
        self.fields["permission_manage_donations"].help_text = (
            "Allow the user to add and email vendors and manage club donations"
            if club and club.enable_donation_tracking
            else "Donation tracking is off right now, enable it in setup"
        )
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = True
        self.helper.layout = Layout(
            *self.Meta.fields,
            Div(
                HTML(
                    '<button type="button" class="btn btn-secondary" onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                ),
                HTML(
                    f'<button hx-post="{post_url}" hx-target="#modals-here" hx-include="closest form" type="button" class="btn btn-primary ms-2">Save</button>'
                ),
                css_class="modal-footer",
            ),
        )


class ClubTreasurerReportForm(forms.Form):
    start_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        required=False,
    )
    end_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        add_bootstrap_classes(self)

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")
        if start_date and end_date and start_date > end_date:
            self.add_error("end_date", "End date must be on or after the start date.")
        return cleaned_data


class ClubMoneyForm(forms.ModelForm):
    class Meta:
        model = ClubMoney
        fields = ["date", "amount", "description", "category"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, category_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        add_bootstrap_classes(self)
        if category_choices is not None:
            self.fields["category"].choices = category_choices


class ClubMoneyBalanceForm(forms.Form):
    account_balance = forms.DecimalField(
        max_digits=10, decimal_places=2, label="Enter your actual bank account balance"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        add_bootstrap_classes(self)


class ClubMemberMergeTargetForm(forms.Form):
    target = forms.CharField(
        widget=autocomplete.Select2(
            url="club-member-merge-autocomplete",
            forward=["club_slug", "exclude_member"],
            attrs={
                "data-html": True,
                "data-container-css-class": "",
            },
        )
    )
    club_slug = forms.CharField(widget=HiddenInput())
    exclude_member = forms.IntegerField(widget=HiddenInput(), required=False)

    def __init__(self, club, source, *args, **kwargs):
        self.club = club
        self.source = source
        super().__init__(*args, **kwargs)
        self.fields["target"].label = f"Merge {self.source} with"
        self.fields["club_slug"].initial = club.slug
        self.fields["exclude_member"].initial = source.pk
        add_bootstrap_classes(self)

    def clean_target(self):
        target_pk = self.cleaned_data["target"]
        try:
            target = ClubMember.objects.get(pk=target_pk, club=self.club)
        except ClubMember.DoesNotExist as exc:
            msg = "Select a member from this club"
            raise forms.ValidationError(msg) from exc
        if target == self.source:
            msg = "You can't merge a member with themselves"
            raise forms.ValidationError(msg)
        return target


class ClubMemberMergeReviewForm(MarksClubMemberAdminEditedMixin, forms.ModelForm):
    class Meta:
        model = ClubMember
        fields = ["name", "email", "phone_number", "address"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        add_bootstrap_classes(self)


class VolunteerJobForm(forms.ModelForm):
    """Admin form to ask for help with a job (Part 7). Bounty blank = volunteer."""

    class Meta:
        model = VolunteerJob
        fields = ["description", "bounty", "people_needed"]
        labels = {
            "description": "Job",
            "bounty": "Bounty",
            "people_needed": "How many people do you need?",
        }
        help_texts = {
            "bounty": "Leave blank for volunteer. Add an invoice adjustment to pay people for signing up for this job",
            "people_needed": "Jobs are first come, first serve",
        }
        widgets = {
            "description": forms.TextInput(attrs={"placeholder": "What do you need help with?"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["people_needed"].initial = 1
        self.fields["bounty"].required = False
        add_bootstrap_classes(self)

    def clean_people_needed(self):
        value = self.cleaned_data["people_needed"]
        if value < 1:
            msg = "You need at least one person"
            raise forms.ValidationError(msg)
        return value


class SpeakerForm(forms.ModelForm):
    """Add or edit a speaker in the directory.

    Anyone with a permission in an NEC club can add a speaker, and the speaker doesn't need
    an account here -- most of them don't have one.  Only `name` is required; the rest is
    whatever the person filling it in happens to know.

    No club picker: `Speaker.club` is still recorded, but SpeakerCreateView works it out from
    the club whose page the person came from, or their only NEC club.  Asking made the form
    longer to answer a question almost nobody had a second answer to -- the same reasoning as
    SpeakerCommentView, which has never asked either.
    """

    class Meta:
        model = Speaker
        fields = [
            "name",
            "image",
            "url",
            "bio",
            "programs",
            "topics",
            "email",
            "phone",
            "website",
            "facebook_page",
            "location",
            "location_coordinates",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Jane Aquarist"}),
            "bio": forms.Textarea(attrs={"rows": 5, "placeholder": "A paragraph about the speaker."}),
            "programs": forms.Textarea(
                attrs={"rows": 4, "placeholder": "One talk per line, or however they list them."}
            ),
            "topics": forms.SelectMultiple(attrs={"size": 10}),
            "website": forms.URLInput(attrs={"placeholder": "https://example.com"}),
            "facebook_page": forms.URLInput(attrs={"placeholder": "https://www.facebook.com/..."}),
            "location": forms.TextInput(attrs={"placeholder": "Providence, RI"}),
        }
        help_texts = {
            "location": "Roughly where they travel from. Used for the map and the distance filter.",
            "programs": "The talks they offer.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["topics"].required = False
        self.fields["topics"].queryset = SpeakerTopic.objects.all()
        # Topics are a closed vocabulary (see auctions/speaker_topics.py). Nothing here creates
        # one, which is what stops the list drifting into three spellings of "cichlids" again.
        self.fields["topics"].help_text = "Pick everything they talk about. Use Other if nothing fits."
        # A speaker with no location can't appear on the map or in the distance filter, which is
        # most of what the directory is for -- so it's required when adding someone. Editing an
        # imported speaker who never had one stays possible.
        if not (self.instance and self.instance.pk):
            self.fields["location"].required = True
        self.fields["url"].required = False
        # Marking the input image-only lets the app's WebView file chooser offer the camera; not
        # setting `capture` keeps the photo library available too. Copied from CreateImageForm.
        self.fields["image"].widget.attrs["accept"] = "image/*"
        self.helper = FormHelper()
        self.helper.form_method = "post"
        # Required or the photo silently doesn't upload.
        self.helper.attrs = {"enctype": "multipart/form-data"}
        self.helper.layout = Layout(
            "name",
            Fieldset(
                "Photo",
                "image",
                "url",
            ),
            "bio",
            "programs",
            Fieldset(
                "Topics",
                "topics",
            ),
            Fieldset(
                "Contact",
                Div(
                    Div("email", css_class="col-md-6"),
                    Div("phone", css_class="col-md-6"),
                    css_class="row",
                ),
                "website",
                "facebook_page",
            ),
            Fieldset(
                "Location",
                "location",
                "location_coordinates",
            ),
        )
        self.helper.add_input(Submit("submit", "Save speaker", css_class="btn-primary"))
        add_bootstrap_classes(self)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            msg = "Please enter the speaker's name"
            raise forms.ValidationError(msg)
        return name

    def clean_url(self):
        """Validate that the URL points to an image. Same check LotImage's form runs."""
        url = self.cleaned_data.get("url")
        if not url:
            return url
        return validate_image_url(url)

    def clean_image(self):
        """Reject a corrupt upload with a friendly message instead of a 500 during thumbnailing.

        Django's ImageField only runs Pillow's header check, which lets truncated files through
        to blow up later. See CreateImageForm.clean_image -- this is the same guard.
        """
        image = self.cleaned_data.get("image")
        # Only a freshly uploaded file needs decoding; an unchanged stored image is fine.
        if isinstance(image, UploadedFile):
            validate_uploaded_image(image)
        return image


class SpeakerCommentForm(forms.ModelForm):
    """Leave a note about a speaker on their panel."""

    class Meta:
        model = SpeakerComment
        fields = ["body"]
        labels = {"body": ""}
        widgets = {
            "body": forms.Textarea(
                attrs={"rows": 3, "placeholder": "How did the talk go? Anything another club should know?"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].required = True
        add_bootstrap_classes(self)

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            msg = "Please write something first"
            raise forms.ValidationError(msg)
        return body


class ClubDonationSettingsForm(forms.ModelForm):
    """Configure donation tracking for a club: whether it's on, and how mail goes out."""

    class Meta:
        model = Club
        fields = [
            "enable_donation_tracking",
            "donation_email_mode",
            "donation_followup_days",
            "donation_context",
            "donation_mailing_address",
        ]
        widgets = {
            "donation_email_mode": forms.RadioSelect(),
            "donation_context": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "nonprofit id number or other information to use in the context of outgoing emails",
                }
            ),
            "donation_mailing_address": forms.Textarea(
                attrs={"rows": 3, "placeholder": "Club name\n123 Main St\nCity, State 12345"}
            ),
        }

    def __init__(self, *args, routing_enabled=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.fields["enable_donation_tracking"].label = "Enable donation tracking"
        # The terms live in a modal on the settings page (club_donation_settings.html): a club is
        # agreeing to them by switching this on, so they have to be readable from right here.
        self.fields["enable_donation_tracking"].help_text = mark_safe(  # noqa: S308 - literal
            "Track which vendors you've asked for donations, and what they said. By using this "
            "feature you confirm that your club has read and accepted the "
            '<a href="#" data-bs-toggle="modal" data-bs-target="#donation-terms-modal">terms and '
            "conditions</a> that go along with it."
        )
        self.fields["donation_email_mode"].label = "How to send donation emails"
        # RadioSelect renders each choice's label, so the routed option has to name the real
        # address here rather than leaning on help_text the way the other fields do.
        club = self.instance
        # Must match DONATION_ALIAS_RE in email_routing.py: the club slug leads, matching the
        # existing -auctions / -contact aliases, and the trailing digits identify the vendor.
        routed_example = (
            f"{club.slug}-donations-1234567890@{settings.EMAIL_ROUTING_DOMAIN}"
            if club and club.pk and settings.EMAIL_ROUTING_DOMAIN
            else "a tracked address on this site"
        )
        # mark_safe so each option can carry its own help line underneath. Django escapes choice
        # labels with conditional_escape, so a SafeString passes through and anything else can't
        # inject markup. Nothing user-supplied is interpolated here.
        self.fields["donation_email_mode"].choices = [
            (
                Club.DONATION_EMAIL_MODE_ROUTED,
                mark_safe(  # noqa: S308 - both branches are literals built from settings, not user input
                    f"Send mail from this site<br><small class='text-muted'>Sent as {escape(routed_example)}, "
                    "so replies are tracked against the vendor</small>"
                ),
            ),
            (
                Club.DONATION_EMAIL_MODE_COPY,
                mark_safe(  # noqa: S308 - literal
                    "Copy/paste to my email<br><small class='text-muted'>No way to track replies</small>"
                ),
            ),
        ]
        if not routing_enabled:
            # Without SES routing there is no tracked address to send from, so copy/paste is the
            # only mode that can work. Leave it visible but fixed rather than silently switching.
            self.fields["donation_email_mode"].disabled = True
            self.fields["donation_email_mode"].help_text = (
                "Email routing is not enabled on this site, so donation emails have to be "
                "copy/pasted into your own email program."
            )
            self.initial["donation_email_mode"] = Club.DONATION_EMAIL_MODE_COPY
        add_bootstrap_classes(self)
        # form-select on a radio group makes each radio render as a dropdown-sized box.
        self.fields["donation_email_mode"].widget.attrs["class"] = "form-check-input"

    def clean_donation_email_mode(self):
        # A disabled field returns its initial value, but be explicit: nothing should be able to
        # persist "send from this site" on an install that has no address to send from.
        mode = self.cleaned_data.get("donation_email_mode")
        if not settings.SES_ROUTE_EMAILS_ENABLED:
            return Club.DONATION_EMAIL_MODE_COPY
        return mode

    def clean(self):
        # Every donation email carries the club's postal address, whichever way it goes out: US
        # law wants one on a solicitation sent in bulk, and donations.send_request refuses without
        # it. Ask for it here, where it can still be typed, rather than at the end of the contact
        # dialog once an admin has written an email they can't send.
        cleaned_data = super().clean()
        if (
            cleaned_data.get("enable_donation_tracking")
            and not (cleaned_data.get("donation_mailing_address") or "").strip()
        ):
            self.add_error(
                "donation_mailing_address",
                "Donation emails have to carry a postal address for the club, so this is required "
                "while donation tracking is on.",
            )
        return cleaned_data


class DonationVendorForm(forms.ModelForm):
    """Add or edit a vendor. Rendered in the modal that opens from the vendor's name."""

    #: A plain ``<input type="date">`` rather than the site's usual DateTimePickerInput. That
    #: widget wires itself up on DOMContentLoaded, which has long since fired by the time HTMX
    #: swaps this form into a modal, so its calendar never appeared here. The native control needs
    #: no JavaScript at all, and gives phones their own date wheel.
    followup_due = forms.DateField(
        required=False,
        label="Follow up on",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="When this vendor should show up as needing a nudge.",
    )

    class Meta:
        model = DonationVendor
        fields = ["name", "contact_name", "email", "status", "followup_due", "context"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Business name"}),
            "contact_name": forms.TextInput(attrs={"placeholder": "Who you talk to there"}),
            "email": forms.EmailInput(attrs={"placeholder": "email@example.com"}),
            "context": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "What they sell, past donations, who introduced you — passed to the "
                    "language model when it writes an email",
                }
            ),
        }
        help_texts = {
            "context": "Better context, better results.",
            "email": "",
            "status": "",
        }

    def __init__(self, *args, post_url=None, club=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._club = club
        self.helper = FormHelper()
        self.helper.form_method = "post"
        if post_url:
            self.helper.form_action = post_url
        self.fields["contact_name"].required = False
        self.fields["email"].required = False
        vendor = self.instance if self.instance and self.instance.pk else None
        if vendor and vendor.followup_due:
            # The stored value is a datetime; a date input can only render "YYYY-MM-DD", and the
            # day it belongs to is the one the club would read off a calendar, not the UTC one.
            self.initial["followup_due"] = timezone.localtime(vendor.followup_due).date()
        if vendor and vendor.unsubscribed:
            # The vendor asked us to stop. A club admin editing this row must not be able to
            # click the status back to something contactable.
            self.fields["status"].disabled = True
            self.fields[
                "status"
            ].help_text = "This vendor unsubscribed. They cannot be contacted again from any club on this site."
            self.fields["email"].disabled = True
        add_bootstrap_classes(self)
        base_fields = ["name", "contact_name", "email", "status", "followup_due", "context"]
        if not vendor:
            # Nothing has happened to a vendor being typed in for the first time, so there is no
            # date to choose: save() starts their clock today, which puts them straight onto the
            # follow-up list as somebody who still needs a first email.
            del self.fields["followup_due"]
            base_fields.remove("followup_due")
        if post_url:
            # Submitted over HTMX, like the club member and AuctionTOS modals: the POST answers
            # with the script that closes the modal, which only makes sense swapped into the page.
            # A plain Submit here would navigate to that script as if it were a page.
            self.helper.layout = Layout(
                *base_fields,
                Div(
                    HTML(
                        '<button type="button" class="btn btn-secondary" '
                        'onmousedown="event.preventDefault()" onclick="closeModal()">Cancel</button>'
                    ),
                    HTML(
                        f'<button hx-post="{post_url}" hx-target="#modals-here" hx-include="closest form" '
                        'type="button" class="btn btn-primary ms-2">Save</button>'
                    ),
                    css_class="modal-footer",
                ),
            )
        else:
            self.helper.layout = Layout(*base_fields)
            self.helper.add_input(Submit("submit", "Save", css_class="btn-primary"))

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email or not self._club:
            return email
        duplicates = DonationVendor.objects.filter(club=self._club, email=email, is_deleted=False)
        if self.instance and self.instance.pk:
            duplicates = duplicates.exclude(pk=self.instance.pk)
        existing = duplicates.first()
        if existing:
            msg = f"{existing.name} already uses this email address."
            raise forms.ValidationError(msg)
        return email

    def clean_followup_due(self):
        """Turn the picked day into the datetime the model stores.

        Pinned to the start of that day locally, so a vendor picked for today reads as due today
        rather than at some hour of it.
        """
        day = self.cleaned_data.get("followup_due")
        if not day:
            return None
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min), timezone.get_current_timezone())

    def save(self, commit=True):
        creating = not self.instance.pk
        vendor = super().save(commit=False)
        if self._club and not vendor.club_id:
            vendor.club = self._club
        if creating and not vendor.followup_due:
            vendor.followup_due = timezone.now()
        if commit:
            vendor.save()
        return vendor


class DonationContactForm(forms.Form):
    """Step one of the contact dialog: what the model should know before it writes."""

    #: What the "last email" box holds, when it was filled in from the vendor's history.
    KNOWN_DIRECTIONS = (DonationEmail.DIRECTION_INCOMING, DonationEmail.DIRECTION_OUTGOING)

    context = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        label="Context",
        help_text=(
            "Information about what this vendor does, any history of donations, or relevant "
            "information. This will be passed to the LLM; better context, better results."
        ),
    )
    last_email = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6}),
        label="Last email",
        help_text="Paste their last message here if you've been emailing them outside this site.",
    )
    #: Which way the prefilled message went. Carried through the POST because the prompt reads
    #: differently for each: a reply of theirs is something to answer, and a request of ours they
    #: ignored is something to nudge about. Anything unrecognised means "typed in by hand", never a
    #: validation error -- the box is hidden, so an error on it would be an invisible dead end.
    last_email_direction = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A prefilled box is not the same box as an empty one: the label has to say where the text
        # came from, or an admin reads "paste their last message here" over the top of an email
        # they are already looking at and wonders what it wants from them.
        direction = self._direction()
        if direction == DonationEmail.DIRECTION_INCOMING:
            self.fields["last_email"].label = "Their last message"
            self.fields["last_email"].help_text = "From this vendor's history. The email will reply to it."
        elif direction == DonationEmail.DIRECTION_OUTGOING:
            self.fields["last_email"].label = "The last email you sent them"
            self.fields[
                "last_email"
            ].help_text = "From this vendor's history — they haven't replied. The email will follow it up."
        add_bootstrap_classes(self)

    def _direction(self):
        source = self.data if self.is_bound else self.initial
        value = (source.get("last_email_direction") or "").strip()
        return value if value in self.KNOWN_DIRECTIONS else ""

    def clean_last_email_direction(self):
        return self._direction()


class DonationEmailEditForm(forms.Form):
    """Step two: the generated email, before it is sent or copied."""

    subject = forms.CharField(max_length=200, widget=forms.TextInput())
    # Seven rows, not sixteen: the dialog's Send and Copy buttons sit under this box, and a taller
    # one pushed them off the bottom of the modal on an ordinary laptop window. The box scrolls.
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 7}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        add_bootstrap_classes(self)

    def clean_body(self):
        body = (self.cleaned_data.get("body") or "").strip()
        if not body:
            msg = "The email can't be empty"
            raise forms.ValidationError(msg)
        return body
