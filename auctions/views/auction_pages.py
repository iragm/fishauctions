"""The auction as a thing you join: the TOS, creating one, and the auction's own page.

``AuctionTOSAdmin`` and ``AuctionTOSDelete`` are the admin's view of who has joined;
``AuctionCreateView`` and ``AuctionInfo`` are the auction's setup and its public front page.
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.sites.models import Site
from django.db import transaction
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    HttpResponseRedirect,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import (
    CreateView,
    FormMixin,
)

from auctions.forms import (
    AuctionJoin,
    AuctionTOSMergeReviewForm,
    AuctionTOSMergeTargetForm,
    CreateAuctionForm,
    CreateEditAuctionTOS,
    DeleteAuctionTOS,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    ClubHistory,
    ClubMember,
    Invoice,
    Lot,
    LotHistory,
    PickupLocation,
)
from auctions.services import (
    AUCTION_FIELDS_TO_CLONE,
    DEFAULT_AUCTION_DESCRIPTION,
    clone_auction,
    finish_new_auction,
    join_auction,
)

from .base import AuctionViewMixin, _ensure_invoice_renewal_state, _find_club_member, close_modal_response

logger = logging.getLogger(__name__)


class AuctionTOSDelete(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Delete AuctionTOSs"""

    template_name = "auctions/auctiontos_confirm_delete.html"
    merge_template_name = "auctions/contact_merge.html"
    form_class = DeleteAuctionTOS
    model = AuctionTOS
    allow_non_admins = True

    def dispatch(self, request, *args, **kwargs):
        pk = kwargs.pop("pk")
        self.auctiontos = AuctionTOS.objects.filter(pk=pk).first()
        if not self.auctiontos:
            raise Http404
        self.auction = self.auctiontos.auction
        _ = self.can_add_edit_people
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["auctiontos"] = self.auctiontos
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["auctiontos"] = self.auctiontos
        context["tooltip"] = ""
        context["modal_title"] = f"Delete {self.auctiontos.name}"
        return context

    def _is_merge_action(self):
        return self.request.GET.get("action") == "merge" or self.request.POST.get("action") == "merge"

    def _merge_success_url(self):
        return reverse("auction_tos_list", kwargs={"slug": self.auctiontos.auction.slug})

    def _merge_label(self, auctiontos):
        return f"{auctiontos.name} (bidder #{auctiontos.bidder_number})"

    @staticmethod
    def _is_merge_empty(value):
        return value in (None, "")

    def _get_review_initial(self, source, target, form_class):
        form = form_class(instance=target, auction=self.auction)
        initial = {}
        for field_name in form.fields:
            target_value = getattr(target, field_name, None)
            source_value = getattr(source, field_name, None)
            if self._is_merge_empty(target_value) and not self._is_merge_empty(source_value):
                initial[field_name] = source_value.pk if hasattr(source_value, "pk") else source_value
        return initial

    @staticmethod
    def _format_merge_value(value):
        if value in (None, ""):
            return "—"
        return value

    def _build_merge_rows(self, source, target, form):
        rows = []
        for field_name, field in form.fields.items():
            rows.append(
                {
                    "label": field.label,
                    "source_value": self._format_merge_value(getattr(source, field_name, None)),
                    "target_value": self._format_merge_value(getattr(target, field_name, None)),
                }
            )
        return rows

    def _render_merge_select(self, request, form):
        return render(
            request,
            self.merge_template_name,
            {
                "step": "select",
                "page_title": f"Merge user — {self.auctiontos.name}",
                "heading": "Merge user",
                "subheading": f"Auction: {self.auction}",
                "selection_form": form,
                "source_label": self._merge_label(self.auctiontos),
                "cancel_url": self._merge_success_url(),
                "action_url": request.get_full_path(),
                "action_mode": "merge",
            },
        )

    def _render_merge_review(self, request, target, form):
        return render(
            request,
            self.merge_template_name,
            {
                "step": "review",
                "page_title": f"Merge user — {self.auctiontos.name}",
                "heading": "Merge user",
                "subheading": f"Auction: {self.auction}",
                "source": self.auctiontos,
                "target": target,
                "source_label": self._merge_label(self.auctiontos),
                "target_label": self._merge_label(target),
                "review_form": form,
                "comparison_rows": self._build_merge_rows(self.auctiontos, target, form),
                "summary_lines": [
                    f"{self._merge_label(self.auctiontos)} will be deleted.",
                    f"{self._merge_label(target)} will be kept.",
                    "Won lots, sold lots, invoice adjustments, and payments will move to the kept user.",
                ],
                "target_field_name": "target",
                "cancel_url": self._merge_success_url(),
                "action_url": request.get_full_path(),
                "action_mode": "merge",
                "save_button_label": f"Save and delete {self.auctiontos.name}",
            },
        )

    def _get_merge_target(self, target_pk):
        return get_object_or_404(AuctionTOS, pk=target_pk, auction=self.auction)

    def get(self, request, *args, **kwargs):
        if self._is_merge_action():
            form = AuctionTOSMergeTargetForm(self.auctiontos, self.auction)
            return self._render_merge_select(request, form)
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if self._is_merge_action():
            if request.POST.get("step") == "review":
                target = self._get_merge_target(request.POST.get("target"))
                review_form = AuctionTOSMergeReviewForm(request.POST, instance=target, auction=self.auction)
                if review_form.is_valid():
                    with transaction.atomic():
                        # Merge (which deletes the source) BEFORE saving the reviewed fields onto the
                        # target. The review form typically copies the source's email onto the target,
                        # and saving the target with that email while the source still exists trips
                        # AuctionTOS.save()'s exact-email auto-merge — which keeps the *older* record
                        # (the source) and deletes the target out from under us, raising
                        # "Unsaved model instance ... cannot be used in an ORM query" on the next line
                        # (and merging in the wrong direction). Deleting the source first makes the
                        # email unique so the auto-merge can't fire.
                        target.merge_duplicate(
                            self.auctiontos,
                            reason=f"merged by {request.user.username}",
                            user=request.user,
                            preserve_missing_fields=False,
                        )
                        target = review_form.save()
                    messages.success(request, f"Merged {self.auctiontos.name} into {target.name}.")
                    return redirect(self._merge_success_url())
                return self._render_merge_review(request, target, review_form)
            selection_form = AuctionTOSMergeTargetForm(self.auctiontos, self.auction, request.POST)
            if selection_form.is_valid():
                target = selection_form.cleaned_data["target"]
                review_form = AuctionTOSMergeReviewForm(
                    instance=target,
                    initial=self._get_review_initial(self.auctiontos, target, AuctionTOSMergeReviewForm),
                    auction=self.auction,
                )
                return self._render_merge_review(request, target, review_form)
            return self._render_merge_select(request, selection_form)
        form = self.get_form()
        if form.is_valid():
            success_url = reverse("auction_tos_list", kwargs={"slug": self.auctiontos.auction.slug})
            # Deleting an AuctionTOS cascades away its invoice, adjustments, and payments.
            # Block that when an invoice exists; a merge (which moves that history to another
            # user) is required instead. The form already enforces this, but guard here too
            # since this is where the irreversible delete happens.
            performing_merge = bool(form.cleaned_data.get("merge_with")) and not form.cleaned_data.get("delete_lots")
            if not performing_merge and Invoice.objects.filter(auctiontos_user=self.auctiontos).exists():
                messages.error(
                    request,
                    f"{self.auctiontos.name} has an invoice, so deleting them would erase their payment history. "
                    "Merge them into another user instead.",
                )
                return self.form_invalid(form)
            if form.cleaned_data["delete_lots"]:
                sold_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_seller=self.auctiontos)
                won_lots = Lot.objects.exclude(is_deleted=True).filter(auctiontos_winner=self.auctiontos)
                for lot in sold_lots:
                    lot.delete()
                for lot in won_lots:
                    LotHistory.objects.create(
                        lot=lot,
                        user=request.user,
                        message=f"{request.user.username} has removed {self.auctiontos} from this auction, this lot no longer has a winner.",
                        notification_sent=True,
                        bid_amount=0,
                        changed_price=True,
                        seen=True,
                    )
                    lot.auctiontos_winner = None
                    lot.winning_price = None
                    lot.active = True
                    lot.save()
                self.auction.create_history(
                    applies_to="USERS", action=f"Deleted {self.auctiontos.name}", user=request.user
                )
                self.auctiontos.delete()
            elif form.cleaned_data["merge_with"]:
                new_auctiontos = AuctionTOS.objects.get(pk=form.cleaned_data["merge_with"])
                new_auctiontos.merge_duplicate(
                    self.auctiontos, reason=f"merged by {request.user.username}", user=request.user
                )
            else:
                # No lots to delete and no merge target selected; delete this AuctionTOS
                self.auction.create_history(
                    applies_to="USERS", action=f"Deleted {self.auctiontos.name}", user=request.user
                )
                self.auctiontos.delete()
            return redirect(success_url)
        else:
            return self.form_invalid(form)


class AuctionTOSAdmin(LoginRequiredMixin, TemplateView, FormMixin, AuctionViewMixin):
    """Creation and management for AuctionTOSs"""

    template_name = "auctions/generic_admin_form.html"
    form_class = CreateEditAuctionTOS
    model = AuctionTOS
    allow_non_admins = True  # we gate via can_add_edit_people for finer control

    def dispatch(self, request, *args, **kwargs):
        # this can be an int if we are updating, or a string (auction slug) if we are creating
        pk = kwargs.pop("pk")
        self.is_edit_form = True
        try:
            self.auctiontos = AuctionTOS.objects.get(pk=pk)
        except Exception:
            self.auctiontos = None
        if self.auctiontos:
            self.auction = self.auctiontos.auction
        else:
            try:
                self.auction = Auction.objects.get(slug=pk, is_deleted=False)
                self.is_edit_form = False
            except Auction.DoesNotExist:
                raise Http404
        _ = self.can_add_edit_people  # raises PermissionDenied if not allowed
        if self.auction.is_club_managed:
            # In club-managed mode, member details are edited in the club admin, not here.
            if self.is_edit_form and self.auctiontos and self.auctiontos.clubmember_id:
                target = reverse("clubmember_admin", kwargs={"pk": self.auctiontos.clubmember_id})
                target += f"?tos={self.auctiontos.pk}"
                return redirect(target)
            if not self.is_edit_form:
                # Creating a new user — redirect to club member create form.
                target = reverse("clubmember_create", kwargs={"slug": self.auction.club.slug})
                if self.auction.manage_users_through_club == "checkin":
                    target += f"?auction={self.auction.slug}"
                return redirect(target)
            # Editing an existing TOS that has no club member link (e.g. added before club
            # management was enabled) — fall through and show the regular AuctionTOS form.
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["auction"] = self.auction
        form_kwargs["is_edit_form"] = self.is_edit_form
        form_kwargs["auctiontos"] = self.auctiontos
        # Pre-populate new-user form from GET params (name, email, phone)
        if not self.is_edit_form and self.request.method == "GET":
            prefill = {}
            for field in ("name", "email", "phone"):
                val = self.request.GET.get(field, "").strip()
                if val:
                    prefill[field if field != "phone" else "phone_number"] = val
            if prefill:
                form_kwargs.setdefault("initial", {}).update(prefill)
        return form_kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not self.is_edit_form and self.auction.is_online:
            context["tooltip"] = (
                "This is an online auction: users should join through this site. You probably don't want to add them here."
            )
        # context['new_form'] = CreateEditAuctionTOS(
        #     is_edit_form=self.is_edit_form,
        #     auctiontos=self.auctiontos,
        #     auction=self.auction
        # )
        context["unsold_lot_warning"] = ""
        if self.auctiontos:
            try:
                invoice = self.auctiontos.invoice
                _ensure_invoice_renewal_state(invoice)
                invoice_string = invoice.invoice_summary_short
                context["top_buttons"] = render_to_string("invoice_buttons.html", {"invoice": invoice})
                context["unsold_lot_warning"] = invoice.unsold_lot_warning
            except AttributeError:
                invoice = None
                invoice_string = ""
            context["modal_title"] = f"{self.auctiontos.name} {invoice_string}"
        else:
            context["modal_title"] = "Add new user"
        if self.auctiontos:
            context["invoice"] = self.auctiontos.invoice
            context["is_admin"] = True
        # for real time form validation
        extra_script = "<script>"
        if self.auctiontos:
            extra_script += f"var pk={self.auctiontos.pk};"
        else:
            extra_script += "var pk=null;"
        extra_script += f"""var validation_url = '{reverse("auctiontos_validation", kwargs={"slug": self.auction.slug})}';
                            var csrf_token = '{get_token(self.request)}';"""
        extra_script += """

    function setFieldInvalid(fieldId, message, is_invalid) {
        var field = document.getElementById(fieldId);
        if (!field) return;

        var feedbackId = fieldId + "_feedback";
        var feedback = document.getElementById(feedbackId);

        if (is_invalid) {
            field.classList.add("is-invalid");
            var existing_error = document.getElementById( "error_1_"+fieldId);
            if (existing_error) {
                existing_error.remove();
            }
            if (feedback) {
                feedback.remove();
            }
            feedback = document.createElement("div");
            feedback.id = feedbackId;
            feedback.className = "invalid-feedback";
            field.parentNode.appendChild(feedback);

            feedback.textContent = message;
        } else {
            field.classList.remove("is-invalid");
            if (feedback) {
                feedback.remove();
            }
        }
    }

    function showAutocomplete(response, remove) {
        var feedback = document.getElementById('id_name_feedback');
        if (feedback) {
            feedback.remove();
        }
        if (remove) {
            return;
        }
        feedback = document.createElement("div");
        feedback.id = "id_name_feedback";
        feedback.className = "valid-feedback d-block cursor-pointer";
        var buttonText = response.id_email ? "Click to use " + response.id_email : "Click to fill in details";
        feedback.innerHTML = "<button role='button' class='btn btn-sm btn-info' id='autocompleteTosForm'>" + buttonText + "</button>";
        var autocomplete = response;
        document.getElementById('id_name').parentNode.appendChild(feedback);

        //setTimeout(function() {
            var link = document.getElementById('autocompleteTosForm');
            link.addEventListener('click', function(event) {
            event.preventDefault();

            for (var key in autocomplete) {
                console.log(key);
                if (autocomplete.hasOwnProperty(key)) {
                    var element = document.getElementById(key);
                    if (element) {
                        if (element.type !== "checkbox" && element.value === "") {
                            element.value = autocomplete[key] || '';
                        }
                        if (element.type === "checkbox") {
                            element.checked = autocomplete[key] === true;
                        }
                    }
                }
            }

            });
            link.focus();
        //}, 40);

    }

    function hasAutocompleteData(response) {
        return !!(response.id_email || response.id_address || response.id_phone_number || response.id_memo);
    }


    function setFieldNote(fieldId, message) {
        var field = document.getElementById(fieldId);
        if (!field) return;

        var noteId = fieldId + "_note";
        var note = document.getElementById(noteId);
        if (note) {
            note.remove();
        }

        if (!message) {
            return;
        }

        note = document.createElement("div");
        note.id = noteId;
        note.className = "text-warning small mt-1";
        note.textContent = message;
        field.parentNode.appendChild(note);
    }

    function validateField() {
        var data = {
            pk: pk,
            name: $("#id_name").val(),
            bidder_number: $("#id_bidder_number").val(),
            email: $("#id_email").val(),
        };

        $.ajax({
            url: validation_url,
            type: "POST",
            data: data,
            headers: { "X-CSRFToken": csrf_token },
            success: function (response) {
                if (response.name_tooltip) {
                    setFieldNote("id_name", response.name_tooltip);
                    showAutocomplete(response, true)
                } else if (hasAutocompleteData(response)) {
                    setFieldNote("id_name", "");
                    showAutocomplete(response)
                } else {
                    setFieldNote("id_name", "");
                    showAutocomplete(response, true)
                }
                if (response.email_tooltip) {
                    setFieldInvalid("id_email", response.email_tooltip, true);
                } else {
                    setFieldInvalid("id_email", response.email_tooltip, false);
                }
                if (response.bidder_number_tooltip) {
                    setFieldInvalid("id_bidder_number", response.bidder_number_tooltip, true);
                } else {
                    setFieldInvalid("id_bidder_number", response.bidder_number_tooltip, false);
                }
            }
        });
    }

    $("#id_bidder_number, #id_name, #id_email").on("blur", validateField);
        </script>"""
        context["extra_script"] = mark_safe(extra_script)
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if form.is_valid():
            if self.auctiontos:
                obj = self.auctiontos
                if form.has_changed():
                    self.auction.create_history(
                        applies_to="USERS",
                        action=f"Edited {obj.name}: ",
                        user=request.user,
                        form=form,
                    )
            else:
                obj = AuctionTOS.objects.create(
                    auction=self.auction,
                    pickup_location=form.cleaned_data["pickup_location"],
                    manually_added=True,
                )
                self.auction.create_history(
                    applies_to="USERS",
                    action=f"Added {form.cleaned_data['name']}",
                    user=request.user,
                )
            obj.bidder_number = form.cleaned_data["bidder_number"]
            obj.pickup_location = form.cleaned_data["pickup_location"]
            obj.name = form.cleaned_data["name"]
            obj.email = form.cleaned_data["email"]
            obj.phone_number = form.cleaned_data["phone_number"]
            obj.address = form.cleaned_data["address"]
            obj.is_admin = form.cleaned_data["is_admin"]
            obj.bidding_allowed = form.cleaned_data["bidding_allowed"]
            obj.selling_allowed = form.cleaned_data["selling_allowed"]
            obj.is_club_member = form.cleaned_data["is_club_member"]
            obj.memo = form.cleaned_data["memo"]
            obj.save()
            return close_modal_response("reload-page")
        else:
            name = form.cleaned_data.get("name")
            if not name:
                self.get_form().add_error("name", "Name is required")
            return self.form_invalid(form)


class AuctionConfirmView(LoginRequiredMixin, TemplateView):
    """
    Confirmation page for auction creation - allows user to choose between creating a club auction or selling a single item
    """

    template_name = "auction_confirm.html"

    def dispatch(self, request, *args, **kwargs):
        # Check if user has permission to create auctions
        auction_creation_allowed = False
        if self.request.user.is_authenticated and self.request.user.userdata.can_create_club_auctions:
            auction_creation_allowed = True
        if self.request.user.is_superuser:
            auction_creation_allowed = True
        if not auction_creation_allowed:
            # If user can't create auctions, redirect them directly to selling
            return redirect("selling")
        return super().dispatch(request, *args, **kwargs)


def _add_club_admins_as_auction_tos(auction, requesting_user):
    """Create AuctionTOS admin records for club members with admin/manage_auctions permissions.

    Only runs when the auction has a club and at least one pickup location.
    Skips the requesting user (already an admin as the auction creator).
    """
    if not auction.club:
        return
    default_location = auction.location_qs.first()
    if not default_location:
        return
    manage_auctions_members = (
        ClubMember.objects.filter(
            club=auction.club,
            is_deleted=False,
        )
        .filter(Q(permission_manage_auctions=True) | Q(permission_admin=True))
        .exclude(user=requesting_user)
        .distinct()
    )
    for member in manage_auctions_members:
        existing_tos = None
        if member.user:
            existing_tos = AuctionTOS.objects.filter(auction=auction, user=member.user).first()
        if not existing_tos and member.email:
            existing_tos = AuctionTOS.objects.filter(auction=auction, email=member.email).first()
        if not existing_tos:
            AuctionTOS.objects.create(
                auction=auction,
                user=member.user,
                pickup_location=default_location,
                name=member.display_name,
                email=member.email or "",
                phone_number=member.phone_number or "",
                address=member.address or "",
                is_admin=True,
                manually_added=True,
            )
            auction.create_history(
                applies_to="USERS",
                action=f"Automatically added {member.display_name} as auction admin because of their club role in '{auction.club}'.",
                user=None,
            )


class AuctionCreateView(CreateView, LoginRequiredMixin):
    """
    Creating a new auction
    """

    model = Auction
    template_name = "auction_create_form.html"
    form_class = CreateAuctionForm
    redirect_url = None  # really only used if this is a cloned auction
    cloned_from = None

    #: Auction settings a copy inherits.  The list itself lives in
    #: :data:`auctions.services.AUCTION_FIELDS_TO_CLONE`, because the copy button on this page is
    #: no longer its only caller -- ``palette_actions.create_auction`` makes the same copy for an
    #: agent.  Kept as a class attribute so a test can read it: see
    #: ``tests.AuctionCloneCustomFieldsTests``, which fails if the custom fields form grows a field
    #: the list does not carry.
    fields_to_clone = AUCTION_FIELDS_TO_CLONE

    def dispatch(self, request, *args, **kwargs):
        original_dispatch = super().dispatch(request, *args, **kwargs)
        auction_creation_allowed = False
        if self.request.user.is_authenticated and self.request.user.userdata.can_create_club_auctions:
            auction_creation_allowed = True
        if self.request.user.is_superuser:
            auction_creation_allowed = True
        if not auction_creation_allowed:
            return redirect(reverse("home"))
        return original_dispatch

    def get_success_url(self):
        if self.redirect_url:
            return self.redirect_url
        else:
            messages.success(
                self.request,
                "Auction created!  Now, create a location to exchange lots.",
            )
            return reverse("create_auction_pickup_location", kwargs={"slug": self.object.slug})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "New auction"
        context["new"] = True
        userData = self.request.user.userdata
        # a bit of logic used on auction_create_form.html to suggest auction names
        context["club"] = ""
        club = userData.club
        if club:
            context["club"] = str(club)
            if club.abbreviation:
                context["club"] = club.abbreviation
        if settings.ENABLE_CLUB_FINDER and not club:
            context["show_club_tip"] = True
        return context

    def get_form_kwargs(self, *args, **kwargs):
        kwargs = super().get_form_kwargs(*args, **kwargs)
        kwargs["user"] = self.request.user
        kwargs["user_timezone"] = self.request.COOKIES.get("user_timezone", settings.TIME_ZONE)
        data = self.request.GET.copy()
        self.cloned_from = data.get("copy", None)
        kwargs["cloned_from"] = self.cloned_from
        return kwargs

    def form_valid(self, form, **kwargs):
        """Rules for new auction creation.

        Three buttons post to this, and the querystring says which: ``?clone=true`` copies the
        auction named in ``cloned_from``, ``?online`` makes a fresh online one, and anything else
        makes a fresh in-person one.  The copy itself is :func:`auctions.services.clone_auction`,
        shared with the assistant so an auction copied by asking for one is the same auction as one
        copied by clicking.
        """
        if "clone" in str(self.request.GET):
            source = Auction.objects.filter(slug=form.cleaned_data["cloned_from"], is_deleted=False).first()
            # you still don't get to clone auctions that aren't yours...
            if source and source.permission_check(self.request.user):
                self.object = clone_auction(
                    source,
                    title=form.cleaned_data["title"],
                    date_start=form.cleaned_data["date_start"],
                    created_by=self.request.user,
                )
                # because we will almost certainly have locations, default to the main auction page
                self.redirect_url = self.object.get_absolute_url()
                return HttpResponseRedirect(self.get_success_url())
            # Nothing to copy, or not theirs to copy.  Fall through and make a fresh one rather
            # than 500ing on them: they asked for an auction and they get an auction.
        auction = form.save(commit=False)
        auction.created_by = self.request.user
        auction.promote_this_auction = False  # all auctions start not promoted
        auction.date_start = form.cleaned_data["date_start"]
        auction.is_online = "online" in str(self.request.GET)
        # The model default ("custom") preserves behavior for pre-existing and cloned
        # auctions; brand-new auctions start with the alternate split off.
        auction.alternate_split_mode = "off"
        if not auction.is_online:
            # override default settings for new in-person auctions
            auction.online_bidding = "disable"
            auction.buy_now = "disable"
            auction.reserve_price = "disable"
        else:
            # override default settings for new online auctions
            auction.use_quantity_field = True
        if not auction.summernote_description:
            auction.summernote_description = DEFAULT_AUCTION_DESCRIPTION
        run_duration = timezone.timedelta(days=7)
        if auction.is_online:
            auction.date_end = auction.date_start + run_duration
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_end
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start
        else:
            auction.date_end = None
            if not auction.lot_submission_end_date:
                auction.lot_submission_end_date = auction.date_start
            if not auction.lot_submission_start_date:
                auction.lot_submission_start_date = auction.date_start - run_duration
            if not auction.date_online_bidding_starts:
                auction.date_online_bidding_starts = auction.date_start - run_duration
            if not auction.date_online_bidding_ends:
                auction.date_online_bidding_ends = auction.date_start
        auction.save()
        if not auction.is_online:
            # let's route in-person auctions to the rule page next
            self.redirect_url = auction.get_edit_url()
        finish_new_auction(auction, self.request.user)
        return super().form_valid(form)


class AuctionInfo(FormMixin, DetailView, AuctionViewMixin):
    """Main view of a single auction"""

    template_name = "auction.html"
    model = Auction
    form_class = AuctionJoin
    rewrite_url = None
    auction = None
    allow_non_admins = True

    def get(self, request, *args, **kwargs):
        if self.is_auction_admin:
            if str(request.GET.get("dismissed_promo_banner", "")).lower() in ("1", "true"):
                self.auction.dismissed_promo_banner = True
                self.auction.save()
            if str(request.GET.get("dismissed_customize_event_banner", "")).lower() in ("1", "true"):
                self.auction.dismissed_customize_event_banner = True
                self.auction.save()
            if str(request.GET.get("make_current_auction", "")).lower() in ("1", "true") and self.auction.club_id:
                club = self.auction.club
                club.current_auction = self.auction
                club.save(update_fields=["current_auction"])
                messages.success(request, f"This is now the current auction for {club.name}.")
                return redirect("auction_main", slug=self.auction.slug)
            if request.user.is_superuser:
                if str(request.GET.get("trust_user", "")).lower() in ("1", "true"):
                    self.auction.created_by.userdata.is_trusted = True
                    self.auction.created_by.userdata.save()
                    messages.success(request, f"{self.auction.created_by.username} is now trusted")
                if str(request.GET.get("make_club_admin", "")).lower() in ("1", "true"):
                    creator = self.auction.created_by
                    creator_club = getattr(creator.userdata, "club", None)
                    if creator_club:
                        # Fill contact info from user account; get_or_create won't duplicate.
                        member = ClubMember.objects.filter(club=creator_club, user=creator).first()
                        if not member:
                            member = ClubMember(
                                club=creator_club,
                                user=creator,
                                source="manually_added",
                            )
                        # Always populate contact fields from user data (safe to overwrite blanks).
                        member.name = member.name or creator.get_full_name() or creator.username
                        if not member.email:
                            member.email = creator.email or None
                        if not member.phone_number:
                            member.phone_number = getattr(creator.userdata, "phone_number", None) or None
                        if not member.address:
                            member.address = getattr(creator.userdata, "address", None) or ""
                        # Count the creator's clubless auctions before saving: granting
                        # permission_admin fires the on_club_member_saved signal, which associates
                        # those auctions with the club and books their club ledger. Capturing the
                        # count first keeps the success message accurate.
                        assigned_count = Auction.objects.filter(
                            created_by=creator, club__isnull=True, is_deleted=False
                        ).count()
                        member.permission_admin = True
                        member.save()
                        ClubHistory.objects.create(
                            club=creator_club,
                            user=request.user,
                            action=f"Granted admin permissions to {creator.get_full_name() or creator.username} via auction admin panel"
                            + (f"; assigned {assigned_count} auction(s) to club" if assigned_count else ""),
                            applies_to="MEMBERS",
                        )
                        messages.success(
                            request,
                            f"{creator.username} is now an admin of {creator_club.name}"
                            + (f" and {assigned_count} auction(s) assigned to club" if assigned_count else ""),
                        )
            # created_by is nullable (SET_NULL when an account is deleted, and blank on auctions
            # made before it existed), so this cannot go straight through to .pk -- it 500s the
            # auction page for everyone, not just the creator.
            if self.auction.created_by_id == request.user.pk:
                if str(request.GET.get("enable_online_payments", "")).lower() in ("1", "true"):
                    self.auction.enable_online_payments = True
                    self.auction.save()
                if str(request.GET.get("enable_square_payments", "")).lower() in ("1", "true"):
                    self.auction.enable_square_payments = True
                    self.auction.save()
                if str(request.GET.get("dismissed_paypal_banner", "")).lower() in ("1", "true"):
                    self.auction.dismissed_paypal_banner = True
                    self.auction.save()
                if str(request.GET.get("dismissed_square_banner", "")).lower() in ("1", "true"):
                    self.auction.dismissed_square_banner = True
                    self.auction.save()
                if str(request.GET.get("never_show_paypal_connect", "")).lower() in ("1", "true"):
                    messages.info(
                        request,
                        "You won't see the PayPal connection prompt again.  You can always enable PayPal under Preferences>More>Connect your PayPal account.",
                    )
                    request.user.userdata.never_show_paypal_connect = True
                    request.user.userdata.save()
                if str(request.GET.get("never_show_square_connect", "")).lower() in ("1", "true"):
                    messages.info(
                        request,
                        "You won't see the Square connection prompt again.  You can always enable Square under Preferences>More>Connect your Square account.",
                    )
                    request.user.userdata.never_show_square_connect = True
                    request.user.userdata.save()
        return super().get(request, *args, **kwargs)

    def get_object(self, *args, **kwargs):
        if self.auction:
            self.object = self.auction
        else:
            try:
                auction = Auction.objects.get(slug=self.kwargs.get(self.slug_url_kwarg), is_deleted=False)
                self.auction = auction
                self.object = self.auction
            except Auction.DoesNotExist:
                msg = "No auctions found matching the query"
                raise Http404(msg)
        return self.object

    def get_success_url(self):
        data = self.request.GET.copy()
        try:
            if not data["next"]:
                data["next"] = self.auction.view_lot_link
            return data["next"]
        except Exception:
            return self.auction.view_lot_link

    def get_form_kwargs(self):
        form_kwargs = super().get_form_kwargs()
        form_kwargs["user"] = self.request.user
        form_kwargs["auction"] = self.auction
        form_kwargs["next_url"] = self.request.GET.get("next")
        return form_kwargs

    def dispatch(self, request, *args, **kwargs):
        auction = self.get_object()
        if auction.permission_check(request.user):
            if not auction.all_location_count:
                messages.info(
                    self.request,
                    mark_safe(
                        "You haven't added any pickup locations to this auction yet. <a href='/locations/new/'>Add one now</a>"
                    ),
                    extra_tags="safe",
                )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pickup_locations"] = self.auction.locations
        current_site = Site.objects.get_current()
        context["domain"] = current_site.domain
        context["google_maps_api_key"] = settings.LOCATION_FIELD["provider.google.api_key"]
        # Offer "make this the current club auction" to admins when the auction has a club
        # and isn't already that club's current auction.
        context["can_make_current_auction"] = bool(
            self.auction.club_id and self.is_auction_admin and self.auction.club.current_auction_id != self.auction.pk
        )
        # Show "make club admin" button to superusers when auction creator has a club in their profile.
        # Show when: creator isn't yet an admin of that club, OR this auction has no club assigned yet.
        if self.request.user.is_superuser and self.auction.created_by:
            creator_club = getattr(self.auction.created_by.userdata, "club", None)
            if creator_club:
                creator_is_admin = creator_club.members.filter(
                    user=self.auction.created_by, permission_admin=True, is_deleted=False
                ).exists()
                auction_needs_club = not self.auction.club
                if not creator_is_admin or auction_needs_club:
                    context["can_make_club_admin"] = True
                    context["creator_club"] = creator_club
        if self.auction.closed:
            context["ended"] = True
            messages.info(
                self.request,
                format_html(
                    "This auction has ended.  You can't bid on anything, but you can still <a href='{}'>view lots</a>.",
                    self.auction.view_lot_link,
                ),
                extra_tags="safe",
            )
        else:
            context["ended"] = False

        # Initialize existingTos and i_agree for form
        existingTos = None
        i_agree = False
        existing_club_member = None

        if self.request.user.is_authenticated:
            tos = AuctionTOS.objects.filter(user=self.request.user, auction=self.auction).first()
            existing_club_member = _find_club_member(self.auction.club, self.request.user, self.request.user.email)
            if tos:
                existingTos = tos.pickup_location
                i_agree = True
                context["hasChosenLocation"] = existingTos.pk if existingTos else False
            else:
                context["hasChosenLocation"] = False
                if self.auction.multi_location:
                    i_agree = True
                else:
                    existingTos = PickupLocation.objects.filter(auction=self.auction).first()
        else:
            context["hasChosenLocation"] = False
            if self.auction.multi_location:
                i_agree = True
            else:
                existingTos = PickupLocation.objects.filter(auction=self.auction).first()
        context["show_club_join_message"] = bool(
            self.auction.is_club_managed and self.auction.club and not existing_club_member
        )
        context["active_tab"] = "main"
        # Check if user has lots in this auction
        if self.request.user.is_authenticated:
            context["user_has_lots"] = (
                Lot.objects.exclude(is_deleted=True)
                .filter(auction=self.auction, auctiontos_seller__user=self.request.user)
                .exists()
            )
        else:
            context["user_has_lots"] = False
        # created_by is nullable; see the note on the same comparison in dispatch().
        if self.request.user.is_authenticated and self.request.user.pk == self.auction.created_by_id:
            invalidPickups = self.auction.pickup_locations_before_end
            if invalidPickups:
                messages.info(
                    self.request,
                    format_html(
                        "<a href='{}'>Some pickup times</a> are set before the end date of the auction", invalidPickups
                    ),
                    extra_tags="safe",
                )
            nonLogicalTimes = self.auction.has_non_logical_times
            if nonLogicalTimes:
                messages.info(
                    self.request,
                    format_html(
                        "<a href='{}'>Auction start or end time</a> should be set to a logical time like 14:30 or 09:00",
                        nonLogicalTimes,
                    ),
                    extra_tags="safe",
                )
            if self.auction.time_start_is_at_night and not self.auction.is_online:
                messages.info(
                    self.request,
                    format_html(
                        "You know your auction is starting in the middle of the night, right? <a href='{}'>Click here to change when bidding opens</a> and remember that it's in 24 hour time",
                        reverse("edit_auction", kwargs={"slug": self.auction.slug}),
                    ),
                    extra_tags="safe",
                )

        context["form"] = AuctionJoin(
            user=self.request.user,
            auction=self.auction,
            next_url=self.request.GET.get("next"),
            initial={
                "user": getattr(self.request.user, "id", None),
                "auction": self.auction.pk,
                "pickup_location": existingTos,
                "i_agree": i_agree,
            },
        )
        context["rewrite_url"] = self.rewrite_url
        # Email button: shown to authenticated users when the auction belongs to a club
        if self.auction.club:
            from auctions.email_routing import email_routing_enabled

            if email_routing_enabled():
                context["auction_contact_email"] = self.auction.sender_email
            else:
                context["auction_contact_email"] = self.auction.club.contact_email or None
        else:
            context["auction_contact_email"] = None
        return context

    def post(self, request, *args, **kwargs):
        """Join. The hundred lines that used to live here are ``services.join_auction``.

        Extracted so the assistant can sign somebody up without sending them to this page: the
        rules, the duplicate-record merge, the club member link and the history line are one
        implementation with two callers rather than two that drift.
        """
        auction = self.auction
        form = self.get_form()
        if request.user.is_authenticated and form.is_valid():
            _tos, _created, problem = join_auction(
                request.user,
                auction,
                form.cleaned_data["pickup_location"],
                time_spent_reading_rules=form.cleaned_data["time_spent_reading_rules"],
            )
            if problem == "phone_number":
                messages.error(self.request, "This auction requires a phone number before you can join")
                return redirect(f"{reverse('contact_info')}?next={auction.get_absolute_url()}")
            if problem == "address":
                messages.error(
                    self.request,
                    "You have to set your address before you can choose pickup by mail",
                )
                return redirect(f"{reverse('contact_info')}?next={auction.get_absolute_url()}")
            return self.form_valid(form)
        else:
            logger.debug(form.cleaned_data)
            return self.form_invalid(form)
