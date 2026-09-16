"""Bulk-adding people: bulk add users, and a club's shared spreadsheet.

:class:`CSVContactImportMixin` does column matching for every importer, including the club member
one in :mod:`auctions.views.club_reports`.
"""

import csv
import logging
import re
import uuid
from datetime import date as date_type
from io import TextIOWrapper

import requests
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import transaction
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.forms import modelformset_factory
from django.http import (
    HttpResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.generic.base import ContextMixin

from auctions.forms import (
    QUICK_ADD_TOS_FIELDS,
    QuickAddTOS,
    TOSFormSetHelper,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    normalize_email,
)

from .base import AuctionViewMixin

logger = logging.getLogger(__name__)


class CSVContactImportMixin:
    """Shared CSV parsing for contact imports (AuctionTOS, ClubMember).

    Subclasses implement ``process_csv_data(csv_reader, filename=None)`` and call
    ``handle_csv_upload(csv_file)`` from post().
    """

    EMAIL_FIELD_NAMES = ["email", "e-mail", "email address", "e-mail address"]
    NAME_FIELD_NAMES = ["name", "full name", "first name", "firstname"]
    ADDRESS_FIELD_NAMES = ["address", "mailing address"]
    PHONE_FIELD_NAMES = ["phone", "phone number", "telephone", "telephone number"]
    MEMO_FIELD_NAMES = ["memo", "note", "notes"]
    FIRST_NAME_FIELD_NAMES = ["first name", "firstname", "first"]
    LAST_NAME_FIELD_NAMES = ["last name", "lastname", "last", "surname"]
    MEMBERSHIP_LAST_PAID_FIELD_NAMES = [
        "membership last paid",
        "membership_last_paid",
        "last paid",
        "paid date",
        "paid",
    ]
    MEMBERSHIP_EXPIRATION_FIELD_NAMES = [
        "membership expiration date",
        "membership_expiration_date",
        "expiration date",
        "expiration",
        "expires",
        "membership expires",
    ]
    DISCORD_ID_FIELD_NAMES = ["discord id", "discord_id", "discord"]
    CONTACT_STATUS_FIELD_NAMES = ["contact status", "contact_status", "contact"]
    DATE_JOINED_FIELD_NAMES = ["date joined", "createdon", "created on", "joined", "join date", "date_joined"]

    # Lowercased contact status values -> model values.
    CONTACT_STATUS_MAP = {
        "contact": "contact",
        "contact normally": "contact",
        "non_essential": "non_essential",
        "non essential": "non_essential",
        "no non-essential emails": "non_essential",
        "no non essential emails": "non_essential",
        "do_not_contact": "do_not_contact",
        "do not contact": "do_not_contact",
        "dnc": "do_not_contact",
    }

    @staticmethod
    def parse_contact_status(value):
        """Map a CSV contact status value to a model value, or return None if not recognized."""
        if not value or not value.strip():
            return None
        return CSVContactImportMixin.CONTACT_STATUS_MAP.get(value.strip().lower())

    # A cell matching neither list is "unspecified", not False.
    CSV_TRUE_VALUES = frozenset({"yes", "y", "true", "t", "1", "x", "✓", "checked", "on", "allowed", "enabled"})
    CSV_FALSE_VALUES = frozenset({"no", "n", "false", "f", "0", "unchecked", "off", "blocked", "disabled"})

    @staticmethod
    def parse_csv_boolean(value, extra_true=None):
        """Read a yes/no cell as True, False, or None when unspecified.

        None means use the default on create and leave alone on update. Blank must never mean False: the
        user CSV export leaves "Bidding allowed" blank for everyone who can bid.
        """
        text = (value or "").strip().lower()
        if not text:
            return None
        if extra_true and text in extra_true:
            return True
        if text in CSVContactImportMixin.CSV_TRUE_VALUES:
            return True
        if text in CSVContactImportMixin.CSV_FALSE_VALUES:
            return False
        return None

    @staticmethod
    def parse_flexible_date(value):
        """Parse a date, including partial ones: '2025' → Jan 1 2025, '2025-06' → Jun 1 2025."""
        if not value or not value.strip():
            return None
        value = value.strip()
        if re.match(r"^\d{4}$", value):
            return date_type(int(value), 1, 1)
        m = re.match(r"^(\d{4})[-/](\d{1,2})$", value)
        if m:
            return date_type(int(m.group(1)), int(m.group(2)), 1)
        # ISO format: YYYY-MM-DD
        try:
            return date_type.fromisoformat(value)
        except ValueError:
            pass
        # US format: MM/DD/YYYY or MM-DD-YYYY
        m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})$", value)
        if m:
            try:
                return date_type(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
        # YYYY/MM/DD
        m = re.match(r"^(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})$", value)
        if m:
            try:
                return date_type(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return None

    @staticmethod
    def extract_csv_field(row, field_name_list, default_response=""):
        """The first matching value (case-insensitive) from a lowercase list of field names, or ``default_response``."""
        # A ragged row's surplus cells are under a None key.
        case_insensitive_row = {k.lower(): v for k, v in row.items() if k}
        for name in field_name_list:
            value = case_insensitive_row.get(name)
            if value is not None:
                return value
        return default_response

    @staticmethod
    def csv_columns_exist(field_names, columns):
        """Returns True if any value in the list `columns` exists in the file headers."""
        # A ragged row's surplus cells are under a None key.
        case_insensitive_row = {k.lower() for k in field_names if k}
        for column in columns:
            if column in case_insensitive_row:
                return True
        return False

    def handle_csv_upload(self, csv_file):
        """Parse an uploaded CSV via process_csv_data() and redirect."""
        try:
            csv_file.seek(0)
            csv_reader = csv.DictReader(TextIOWrapper(csv_file.file, encoding="utf-8-sig", newline=""))
            filename = getattr(csv_file, "name", None)
            return self.process_csv_data(csv_reader, filename=filename)
        except (UnicodeDecodeError, ValueError) as e:
            messages.error(
                self.request, f"Unable to read file. Make sure this is a valid UTF-8 CSV file. Error was: {e}"
            )
            return None

    # ------------------------------------------------------------------
    # Preview / confirm framework
    #
    # Importers turn the upload into JSON-serializable planned actions, cached under a one-time
    # token, and show auctions/csv_import_preview.html before writing anything.
    #
    # A subclass implements:
    #   plan_row(self, row) -> dict|None              classify one CSV row
    #   apply_action(self, action, decision) -> str   write one planned action, return a result tag
    #   import_done_url(self) / import_cancel_url(self)
    #   import_target_id(self)                        binds the token to its auction/club
    #   record_import_history(self, results, filename)
    # and sets import_record_kind / import_supports_duplicates / import_preview_columns.
    #
    # Planned-action dict:
    #   {"action": "create"|"update"|"duplicate"|"skip",
    #    "fields": {...},
    #    "target_pk": <existing pk or None>,          # update/duplicate
    #    "target_display": "<existing label>",        # update/duplicate
    #    "match_type": "email"|"name"|None,
    #    "reason": "<why skipped / note>",
    #    "raw": {...original row...}}                 # filled in by build_preview if omitted
    # ------------------------------------------------------------------

    PREVIEW_CACHE_PREFIX = "csv_import"
    PREVIEW_TTL_SECONDS = 60 * 60  # 1 hour
    PREVIEW_TEMPLATE = "auctions/csv_import_preview.html"

    # Subclass overrides
    import_record_kind = "record"
    import_supports_duplicates = False
    import_preview_columns = ()  # list of (header, field_key)
    # A "fields" key that identifies one record within a file (e.g. "email"), to collapse repeats.
    # None where repeats are legitimate.
    import_dedupe_field = None

    def import_target_id(self):
        """A stable id binding a preview token to one auction or club."""
        return

    def _preview_cache_key(self, token):
        return f"{self.PREVIEW_CACHE_PREFIX}:{token}"

    def _dedupe_key(self, action):
        """Key for collapsing same-record rows, or None."""
        field = self.import_dedupe_field
        if not field or action.get("action") == "skip":
            return None
        value = (action.get("fields", {}).get(field) or "").strip().lower()
        return value or None

    @staticmethod
    def _merge_planned_fields(primary, duplicate):
        """Fold a later same-key row into the primary action, filling only unset fields.

        An explicit tri-state ``False`` counts as data. ``present`` flags are OR-ed.
        """
        primary_fields = primary.setdefault("fields", {})
        for key, value in duplicate.get("fields", {}).items():
            if value in (None, ""):
                continue
            if primary_fields.get(key) in (None, ""):
                primary_fields[key] = value
        if "present" in duplicate:
            primary_present = primary.setdefault("present", {})
            for key, value in duplicate["present"].items():
                if value:
                    primary_present[key] = True

    def build_preview(self, csv_reader, filename=None):
        """Plan every row, cache the actions, and return the token."""
        actions = []
        # Dedupe key -> index of the action that owns it.
        primary_by_key = {}
        for raw in csv_reader:
            planned = self.plan_row(raw)
            if planned is None:
                continue
            planned.setdefault("raw", {k: v for k, v in raw.items() if k})
            planned["i"] = len(actions)
            key = self._dedupe_key(planned)
            if key is not None and key in primary_by_key:
                primary = actions[primary_by_key[key]]
                self._merge_planned_fields(primary, planned)
                planned["action"] = "skip"
                planned["reason"] = f"Duplicate {self.import_dedupe_field} in file — combined into an earlier row"
                planned["target_pk"] = None
                planned["target_display"] = ""
                planned["match_type"] = None
            elif key is not None:
                primary_by_key[key] = planned["i"]
            actions.append(planned)
        token = uuid.uuid4().hex
        payload = {
            "view": type(self).__name__,
            "kind": self.import_record_kind,
            "target_id": self.import_target_id(),
            "user_id": self.request.user.pk,
            "filename": filename,
            "actions": actions,
        }
        cache.set(self._preview_cache_key(token), payload, self.PREVIEW_TTL_SECONDS)
        return token

    def load_preview(self, token):
        """The cached payload for *token*, or None if missing, expired, or bound to another user, target or view."""
        if not token:
            return None
        payload = cache.get(self._preview_cache_key(token))
        if not payload:
            return None
        if payload.get("user_id") != self.request.user.pk:
            return None
        if payload.get("target_id") != self.import_target_id():
            return None
        if payload.get("view") != type(self).__name__:
            return None
        return payload

    def clear_preview(self, token):
        if token:
            cache.delete(self._preview_cache_key(token))

    def _hx_aware_redirect(self, url):
        """A redirect that navigates the full page even from an hx-post."""
        if self.request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = url
            return response
        return redirect(url)

    def redirect_to_preview(self, token):
        """Navigate the browser (full page, HTMx-aware) to the preview for *token*."""
        return self._hx_aware_redirect(f"{self.request.path}?preview={token}")

    def render_preview(self, token):
        payload = self.load_preview(token)
        if payload is None:
            messages.error(self.request, "This import preview expired or was not found. Please upload the file again.")
            return redirect(self.import_cancel_url())
        actions = payload["actions"]
        columns = self.import_preview_columns
        summary = {"create": 0, "update": 0, "duplicate": 0, "skip": 0}
        apply_rows = []
        duplicate_rows = []
        skipped_rows = []
        for action in actions:
            kind = action["action"]
            summary[kind] = summary.get(kind, 0) + 1
            # Cells aligned to the columns, since templates can't index a dict by a variable key.
            row = {**action, "cells": [action.get("fields", {}).get(key, "") for _, key in columns]}
            if kind == "duplicate":
                duplicate_rows.append(row)
            elif kind in ("create", "update"):
                apply_rows.append(row)
            elif kind == "skip":
                skipped_rows.append(row)
        context = {
            "token": token,
            "kind": payload["kind"],
            "filename": payload.get("filename"),
            "summary": summary,
            "total": len(actions),
            "columns": columns,
            "supports_duplicates": self.import_supports_duplicates,
            "apply_rows": apply_rows,
            "duplicate_rows": duplicate_rows,
            "skipped_rows": skipped_rows,
            "confirm_url": self.request.path,
            "cancel_url": self.import_cancel_url(),
        }
        return render(self.request, self.PREVIEW_TEMPLATE, context)

    def apply_preview(self, token, post_data):
        payload = self.load_preview(token)
        if payload is None:
            messages.error(self.request, "This import preview expired or was not found. Please upload the file again.")
            return redirect(self.import_cancel_url())
        # cache.add() is SET-NX, so only one concurrent confirm claims the token.
        claim_key = f"{self._preview_cache_key(token)}:applying"
        if not cache.add(claim_key, 1, self.PREVIEW_TTL_SECONDS):
            messages.info(self.request, "This import is already being processed.")
            return redirect(self.import_done_url())
        results = {}
        try:
            # One transaction for the whole batch.
            with transaction.atomic():
                for action in payload["actions"]:
                    decision = post_data.get(f"decision_{action['i']}", "merge")
                    tag = self.apply_action(action, decision)
                    results[tag] = results.get(tag, 0) + 1
        except Exception:
            # Rolled back, so release the claim for a retry.
            cache.delete(claim_key)
            raise
        self.clear_preview(token)
        cache.delete(claim_key)
        self.record_import_history(results, payload.get("filename"))
        self.message_import_results(results)
        return redirect(self.import_done_url())

    def record_import_history(self, results, filename=None):
        """Hook for an audit entry after a confirmed import. No-op by default."""

    def message_import_results(self, results):
        """Flash a summary message after a confirmed import."""
        labels = [
            ("created", "added"),
            ("updated", "updated"),
            ("merged", "merged into existing"),
            ("skipped", "skipped"),
        ]
        parts = [f"{results[tag]} {self.import_record_kind}s {label}" for tag, label in labels if results.get(tag)]
        if parts:
            messages.success(self.request, ", ".join(parts))

    def handle_import_post(self, request, csv_field_names=("csv_file",)):
        """Route confirm and cancel POSTs; returns None for anything else."""
        if request.POST.get("_confirm"):
            return self.apply_preview(request.POST["_confirm"], request.POST)
        if request.POST.get("_cancel"):
            self.clear_preview(request.POST.get("_cancel"))
            return redirect(self.import_cancel_url())
        return None


class BulkAddUsers(LoginRequiredMixin, CSVContactImportMixin, AuctionViewMixin, TemplateView, ContextMixin):
    """Add/edit lots of auctiontos"""

    template_name = "auctions/bulk_add_users.html"
    max_users_that_can_be_added_at_once = 200
    extra_rows = 5
    AuctionTOSFormSet = None
    allow_non_admins = True

    # CSV import preview framework (see CSVContactImportMixin)
    import_record_kind = "user"
    import_supports_duplicates = True
    import_dedupe_field = "email"  # two rows with the same email are the same person; combine them
    import_preview_columns = (
        ("Bidder #", "bidder_number"),
        ("Name", "name"),
        ("Email", "email"),
        ("Phone", "phone"),
    )
    BIDDER_NUMBER_FIELDS = ["bidder number", "bidder", "membernumber", "tempguestnumber"]
    NAME_FIELDS = ["name", "full name", "first name", "firstname"]
    EMAIL_FIELDS = ["email", "e-mail", "email address", "e-mail address"]
    ADDRESS_FIELDS = ["address", "mailing address"]
    PHONE_FIELDS = ["phone", "phone number", "telephone", "telephone number"]
    BIDDING_FIELDS = ["allow bidding", "bidding", "bidding allowed", "allowedtobid"]
    MEMO_FIELDS = ["memo", "note", "notes"]
    ADMIN_FIELDS = ["admin", "staff", "is_admin", "is_staff"]

    def _block_if_club_managed(self):
        if self.auction and self.auction.is_club_managed:
            messages.info(
                self.request,
                "This auction manages users through its club. Add or import members from the club admin page.",
            )
            return redirect(reverse("club_admin", kwargs={"slug": self.auction.club.slug}))
        return None

    def get(self, *args, **kwargs):
        _ = self.can_add_edit_people
        redirected = self._block_if_club_managed()
        if redirected is not None:
            return redirected
        preview_token = self.request.GET.get("preview")
        if preview_token:
            return self.render_preview(preview_token)
        # first, try to read in a CSV file stored in session
        initial_formset_data = self.request.session.get("initial_formset_data", [])
        if initial_formset_data:
            self.extra_rows = len(initial_formset_data) + 1
            del self.request.session["initial_formset_data"]
        else:
            # ?import= copies users from a past auction.
            import_from_auction = self.request.GET.get("import")
            if import_from_auction:
                other_auction = Auction.objects.exclude(is_deleted=True).filter(slug=import_from_auction).first()
                if not other_auction.permission_check(self.request.user):
                    messages.error(
                        self.request,
                        f"You don't have permission to add users from {other_auction}",
                    )
                else:
                    auctiontos = AuctionTOS.objects.filter(auction=other_auction)
                    total_skipped = 0
                    total_tos = 0
                    for tos in auctiontos:
                        # if not self.tos_is_in_auction(self.auction, tos.name, tos.email):
                        if not self.auction.find_user(tos.name, tos.email):
                            initial_formset_data.append(
                                {
                                    "bidder_number": tos.bidder_number,
                                    "name": tos.name,
                                    "phone": tos.phone_number,
                                    "email": tos.email,
                                    "address": tos.address,
                                    "is_club_member": tos.is_club_member,
                                }
                            )
                            total_tos += 1
                        else:
                            total_skipped += 1
                    if total_tos >= self.max_users_that_can_be_added_at_once:
                        messages.error(
                            self.request,
                            f"You can only add {self.max_users_that_can_be_added_at_once} users from another auction at once; run this again to add additional users.",
                        )
                    if total_skipped:
                        messages.info(
                            self.request,
                            f"{total_skipped} users are already in this auction (matched by email, or name if email not set) and do not appear below",
                        )
                    if total_tos:
                        self.extra_rows = total_tos + 1
        self.instantiate_formset()
        self.tos_formset = self.AuctionTOSFormSet(
            form_kwargs={"auction": self.auction, "bidder_numbers_on_this_form": []},
            queryset=self.queryset,
            initial=initial_formset_data,
        )
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def import_target_id(self):
        return f"auction:{self.auction.pk}"

    def import_done_url(self):
        return reverse("auction_tos_list", kwargs={"slug": self.auction.slug})

    def import_cancel_url(self):
        return reverse("bulk_add_users", kwargs={"slug": self.auction.slug})

    @staticmethod
    def _tos_label(tos):
        label = tos.name or "(no name)"
        if tos.bidder_number:
            return f"{label} (bidder #{tos.bidder_number})"
        if tos.email:
            return f"{label} ({tos.email})"
        return label

    def _parse_user_row(self, row):
        """Normalize one CSV row into fields, plus which optional columns the file has.

        The three permission booleans are tri-state; None leaves defaults on create and values on update.
        """
        club_member_fields = ["member", "club member", self.auction.alternative_split_label.lower()]
        is_club_member = self.parse_csv_boolean(
            self.extract_csv_field(row, club_member_fields),
            extra_true={"member", "club member", self.auction.alternative_split_label.lower()},
        )
        bidding_allowed = self.parse_csv_boolean(self.extract_csv_field(row, self.BIDDING_FIELDS))
        is_admin = self.parse_csv_boolean(self.extract_csv_field(row, self.ADMIN_FIELDS))
        fields = {
            "bidder_number": self.extract_csv_field(row, self.BIDDER_NUMBER_FIELDS)[:20],
            "name": self.extract_csv_field(row, self.NAME_FIELDS)[:181],
            "email": normalize_email(self.extract_csv_field(row, self.EMAIL_FIELDS))[:254],
            "phone": self.extract_csv_field(row, self.PHONE_FIELDS)[:20],
            "address": self.extract_csv_field(row, self.ADDRESS_FIELDS)[:500],
            "memo": (self.extract_csv_field(row, self.MEMO_FIELDS) or "")[:500],
            "is_club_member": is_club_member,
            "bidding_allowed": bidding_allowed,
            "is_admin": is_admin,
        }
        cols = list(row.keys())
        # Only non-boolean optional columns need a header-level flag.
        present = {"memo": self.csv_columns_exist(cols, self.MEMO_FIELDS)}
        return fields, present

    def plan_row(self, row):
        fields, present = self._parse_user_row(row)
        name, email = fields["name"], fields["email"]
        base = {"fields": fields, "present": present, "target_pk": None, "target_display": "", "match_type": None}
        if not name and not email:
            return {**base, "action": "skip", "reason": "Row has no name or email"}
        if email:
            existing = self.auction.find_user(email=email)
            if existing:
                return {
                    **base,
                    "action": "update",
                    "target_pk": existing.pk,
                    "target_display": self._tos_label(existing),
                    "match_type": "email",
                    "reason": "Matched an existing user by email",
                }
        if name:
            existing = self.auction.find_user(name=name)
            if existing:
                return {
                    **base,
                    "action": "duplicate",
                    "target_pk": existing.pk,
                    "target_display": self._tos_label(existing),
                    "match_type": "name",
                    "reason": "Same or similar name as an existing user",
                }
        return {**base, "action": "create", "reason": ""}

    def _create_tos(self, fields):
        bidder_number = fields.get("bidder_number", "")
        if bidder_number and AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number).exists():
            bidder_number = ""
        bidding_allowed = fields.get("bidding_allowed")
        create_kwargs = {
            "auction": self.auction,
            "pickup_location": self.auction.location_qs.first(),
            "manually_added": True,
            "bidder_number": bidder_number,
            "name": fields.get("name", ""),
            "phone_number": fields.get("phone", ""),
            "email": fields.get("email", ""),
            "address": fields.get("address", ""),
            "is_club_member": bool(fields.get("is_club_member")),
            "memo": fields.get("memo", ""),
            "is_admin": bool(fields.get("is_admin")),
        }
        if bidding_allowed is not None:
            create_kwargs["bidding_allowed"] = bidding_allowed
        # Unspecified: let AuctionTOS.save() apply the auction's rules.
        tos = AuctionTOS.objects.create(**create_kwargs)
        if bidding_allowed is not None and tos.bidding_allowed != bidding_allowed:
            # Those rules force-allow manually added users, so re-apply an explicit "no".
            tos.bidding_allowed = bidding_allowed
            tos.save(update_fields=["bidding_allowed"])
        return tos

    def _update_tos(self, tos, fields, present):
        """Apply CSV fields to an existing record and return True if anything changed.

        Booleans are only overwritten when the row says so; a non-conflicting CSV bidder number wins.
        """
        changed = False
        name = fields.get("name", "")
        if name and tos.name != name:
            tos.name = name
            changed = True
        phone = fields.get("phone", "")
        if phone and tos.phone_number != phone:
            tos.phone_number = phone
            changed = True
        address = fields.get("address", "")
        if address and tos.address != address:
            tos.address = address
            changed = True
        email = fields.get("email", "")
        if email and not tos.email:
            tos.email = email
            changed = True
        is_club_member = fields.get("is_club_member")
        if is_club_member is not None and tos.is_club_member != is_club_member:
            tos.is_club_member = is_club_member
            changed = True
        bidding_allowed = fields.get("bidding_allowed")
        if bidding_allowed is not None and tos.bidding_allowed != bidding_allowed:
            tos.bidding_allowed = bidding_allowed
            changed = True
        if present.get("memo"):
            memo = fields.get("memo", "")
            if tos.memo != memo:
                tos.memo = memo
                changed = True
        is_admin = fields.get("is_admin")
        if is_admin is not None and tos.is_admin != is_admin:
            tos.is_admin = is_admin
            changed = True
        bidder_number = fields.get("bidder_number", "")
        if bidder_number and tos.bidder_number != bidder_number:
            conflict = (
                AuctionTOS.objects.filter(auction=self.auction, bidder_number=bidder_number).exclude(pk=tos.pk).exists()
            )
            if not conflict:
                tos.bidder_number = bidder_number
                changed = True
        if changed:
            tos.save()
        return changed

    def apply_action(self, action, decision):
        kind = action["action"]
        if kind == "skip":
            return "skipped"
        fields = action.get("fields", {})
        present = action.get("present", {})
        target_pk = action.get("target_pk")
        if kind == "create" or (kind == "duplicate" and decision == "create"):
            tos = self._create_tos(fields)
            if kind == "duplicate" and target_pk:
                # keep the admin-review link to the record it resembles
                AuctionTOS.objects.filter(pk=tos.pk).update(possible_duplicate=target_pk)
                AuctionTOS.objects.filter(pk=target_pk, possible_duplicate__isnull=True).update(
                    possible_duplicate=tos.pk
                )
            return "created"
        # Email match, or a name-match duplicate the admin chose to merge.
        tos = AuctionTOS.objects.filter(pk=target_pk, auction=self.auction).first() if target_pk else None
        if not tos:
            self._create_tos(fields)
            return "created"
        changed = self._update_tos(tos, fields, present)
        if not changed:
            return "unchanged"
        return "updated" if kind == "update" else "merged"

    def record_import_history(self, results, filename=None):
        created = results.get("created", 0)
        updated = results.get("updated", 0) + results.get("merged", 0)
        if not created and not updated:
            return
        parts = []
        if created:
            parts.append(f"{created} users added")
        if updated:
            parts.append(f"{updated} users updated")
        msg = ", ".join(parts)
        if filename:
            msg += f" from {filename}"
        self.auction.create_history(applies_to="USERS", action=msg, user=self.request.user)

    def process_csv_data(self, csv_reader, filename=None, *args, **kwargs):
        """Plan the upload and show the review page; nothing is written yet."""
        fieldnames = csv_reader.fieldnames or []
        recognized = self.csv_columns_exist(
            fieldnames, self.EMAIL_FIELDS + self.NAME_FIELDS + self.PHONE_FIELDS + self.ADDRESS_FIELDS
        )
        if not recognized:
            messages.error(
                self.request,
                "Unable to read information from this CSV file. Make sure it contains an email and a name column.",
            )
            return self._hx_aware_redirect(self.import_cancel_url())
        token = self.build_preview(csv_reader, filename=filename)
        return self.redirect_to_preview(token)

    def post(self, request, *args, **kwargs):
        _ = self.can_add_edit_people
        redirected = self._block_if_club_managed()
        if redirected is not None:
            return redirected
        # Confirm/cancel takes priority over uploads and the formset.
        import_response = self.handle_import_post(request)
        if import_response is not None:
            return import_response
        # Check for CSV file with multiple possible field names
        csv_file = None
        for field_name in ["csv_file", "csv_file_quick"]:
            csv_file = request.FILES.get(field_name)
            if csv_file:
                break

        if csv_file:
            return self.handle_csv_upload(csv_file)
        self.instantiate_formset()
        tos_formset = self.AuctionTOSFormSet(
            self.request.POST,
            form_kwargs={"auction": self.auction, "bidder_numbers_on_this_form": []},
            queryset=self.queryset,
        )
        if tos_formset.is_valid():
            auctiontos = tos_formset.save(commit=False)
            for tos in auctiontos:
                tos.auction = self.auction
                tos.manually_added = True
                tos.save()
            messages.success(self.request, f"Added {len(auctiontos)} users")
            self.auction.create_history(
                applies_to="USERS", action=f"Bulk added {len(auctiontos)} users", user=self.request.user
            )
            return redirect(reverse("auction_tos_list", kwargs={"slug": self.auction.slug}))
        self.tos_formset = tos_formset
        context = self.get_context_data(**kwargs)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["formset"] = self.tos_formset
        context["helper"] = TOSFormSetHelper()
        context["auction"] = self.auction
        context["other_auctions"] = (
            Auction.objects.exclude(is_deleted=True)
            .filter(Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True))
            .exclude(pk=self.auction.pk)
            .distinct()
            .order_by("-date_posted")[:10]
        )
        return context

    def tos_is_in_auction(self, auction, name, email):
        """Deprecated: use auction.find_user()."""
        logger.warning("tos_is_in_auction is deprecated, use auction.find_user() instead")
        qs = AuctionTOS.objects.filter(auction=auction)
        if email:
            qs = qs.filter(email=email)
        elif name:
            qs = qs.filter(Q(name=name, email=None) | Q(name=name, email=""))
        else:
            return None
        return qs.first()

    def dispatch(self, request, *args, **kwargs):
        self.queryset = AuctionTOS.objects.none()  # we don't want to allow editing
        return super().dispatch(request, *args, **kwargs)

    def instantiate_formset(self, *args, **kwargs):
        if not self.AuctionTOSFormSet:
            self.AuctionTOSFormSet = modelformset_factory(
                AuctionTOS,
                extra=self.extra_rows,
                fields=QUICK_ADD_TOS_FIELDS,
                form=QuickAddTOS,
            )


class ImportFromGoogleDrive(LoginRequiredMixin, AuctionViewMixin, TemplateView, ContextMixin):
    """Import users from a Google Drive spreadsheet"""

    template_name = "auctions/import_from_google_drive.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auction"] = self.auction
        return context

    def post(self, request, *args, **kwargs):
        # Check if this is a sync request (no google_drive_link in POST)
        google_drive_link = request.POST.get("google_drive_link", "").strip()

        # If google_drive_link is provided, update it and sync
        if google_drive_link:
            self.auction.google_drive_link = google_drive_link
            self.auction.save()

        # Perform the sync (whether it's a new link or existing link)
        return self.sync_google_drive()

    def sync_google_drive(self):
        """Read data from Google Drive and import users"""
        if not self.auction.google_drive_link:
            messages.error(self.request, "No Google Drive link configured")
            url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
            return redirect(url)

        try:
            # Sharing link (.../d/ID/edit#gid=0) -> export URL (.../d/ID/export?format=csv&gid=0).
            link = self.auction.google_drive_link

            # Extract the spreadsheet ID from the URL
            if "/spreadsheets/d/" in link:
                spreadsheet_id = link.split("/spreadsheets/d/")[1].split("/")[0]
                # Extract gid if present
                gid = "0"
                if "gid=" in link:
                    gid = link.split("gid=")[1].split("&")[0].split("#")[0]
                csv_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?format=csv&gid={gid}"
            else:
                return self._error_redirect("Invalid Google Drive link. Please use a link to a Google Sheets document.")

            # Fetch the CSV data with timeout to prevent hanging
            response = requests.get(csv_url, timeout=30)
            response.raise_for_status()

            csv_reader = csv.DictReader(response.text.splitlines())

            # BulkAddUsers' planning, with the same review page.
            bulk_add_view = BulkAddUsers()
            bulk_add_view.request = self.request
            bulk_add_view.auction = self.auction
            token = bulk_add_view.build_preview(csv_reader, filename="Google Drive sync")

            # User changes happen when the admin confirms.
            self.auction.last_sync_time = timezone.now()
            self.auction.save()
            self.auction.create_history("USERS", action="Pulled Google Drive sheet for import review")
            preview_url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug}) + f"?preview={token}"
            return redirect(preview_url)

        except requests.RequestException as e:
            if "401" in str(e):
                return self._error_redirect(
                    "Unable to fetch data from Google Drive. Make sure the link is shared with 'anyone with the link can view'"
                )
            elif "404" in str(e):
                return self._error_redirect("Link not found or invalid")
            else:
                return self._error_redirect(f"Unable to fetch data from Google Drive. Error was {e}")

        except Exception as e:
            return self._error_redirect(f"An error occurred while importing from Google Drive: {e}")

    def _error_redirect(self, error_message):
        """Helper method to display error and redirect to bulk add users page"""
        messages.error(self.request, error_message)
        url = reverse("bulk_add_users", kwargs={"slug": self.auction.slug})
        return redirect(url)
