"""The reader's own account: profile, username, preferences, notifications, deletion.

``OwnUserDataUpdate`` lists ``SuccessMessageMixin`` first on purpose, or ``UpdateView.form_valid``
wins the MRO and the success message never renders. Preferences and notifications are two forms
partitioning the ``UserData`` fields, which is why neither page needs JavaScript.
"""

import logging
from urllib.parse import quote, unquote

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.messages.views import SuccessMessageMixin
from django.contrib.sites.models import Site
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Q,
)
from django.db.models.base import Model as Model
from django.http import (
    Http404,
    JsonResponse,
)
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import DetailView, TemplateView
from django.views.generic.edit import (
    UpdateView,
)
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from auctions.form_friction import FormFrictionMixin
from auctions.forms import (
    ChangeUsernameForm,
    ChangeUserNotificationsForm,
    ChangeUserPreferencesForm,
    UserLabelPrefsForm,
    UserLocation,
)
from auctions.models import (
    Auction,
    Bid,
    Category,
    ClubMember,
    MobileDevice,
    PageView,
    UserBan,
    UserData,
    UserIgnoreCategory,
    UserLabelPrefs,
)
from auctions.notifications import push_configured
from auctions.services import CONTACT_GATE_NEEDS_PHONE

logger = logging.getLogger(__name__)


class UserView(DetailView):
    """View information about a single user"""

    template_name = "user.html"
    model = User

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["data"] = self.object.userdata
        try:
            context["banned"] = UserBan.objects.get(user=self.request.user.pk, banned_user=self.object.pk)
        except UserBan.DoesNotExist:
            context["banned"] = False
        context["seller_feedback"] = (
            context["data"].my_lots_qs.exclude(feedback_text__isnull=True).order_by("-date_posted")
        )
        context["buyer_feedback"] = (
            context["data"].my_won_lots_qs.exclude(winner_feedback_text__isnull=True).order_by("-date_posted")
        )
        return context


class UserByName(UserView):
    """/user/username storefront view"""

    def dispatch(self, request, *args, **kwargs):
        self.username = kwargs["slug"]
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, *args, **kwargs):
        try:
            return User.objects.get(username=unquote(self.username))
        except User.DoesNotExist:
            pass
        raise Http404


class UsernameUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_username.html"
    model = User
    success_message = "Username updated"
    form_class = ChangeUsernameForm

    def get_object(self, *args, **kwargs):
        try:
            return User.objects.get(pk=self.request.user.pk)
        except User.DoesNotExist:
            raise Http404

    def dispatch(self, request, *args, **kwargs):
        auth = False
        if self.get_object().pk == request.user.pk:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            # "/users/" + str(self.kwargs['pk'])
            data["next"] = reverse("account")
        return data["next"]


class UserLabelPrefsView(UpdateView, SuccessMessageMixin):
    template_name = "user_labels.html"
    model = UserLabelPrefs
    success_message = "Printing preferences updated"
    form_class = UserLabelPrefsForm
    user_pk = None

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
        return data["next"]

    def get_object(self, *args, **kwargs):
        label_prefs, created = UserLabelPrefs.objects.get_or_create(
            user=self.request.user,
            defaults={},
        )
        return label_prefs

    def _show_print_method(self):
        """The print-method dropdown is shown in the app, or on the web to anyone who has registered a device."""
        return bool(self.request.is_mobile_app) or MobileDevice.objects.filter(user=self.request.user).exists()

    def _show_print_from_computer(self):
        """Offer computer-to-phone printing only to an account with a phone that could do it.

        ``ever_print_ready``, not ``print_ready``: the current flag goes False the moment the printer is off,
        and whether it works right now is answered by the last-seen line beside the checkbox.
        ``push_configured`` because the job reaches the phone as an FCM message.
        """
        if not push_configured():
            return False
        return MobileDevice.objects.filter(user=self.request.user, ever_print_ready=True).exists()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["show_print_method"] = self._show_print_method()
        kwargs["is_mobile_app"] = bool(self.request.is_mobile_app)
        kwargs["show_print_from_computer"] = self._show_print_from_computer()
        return kwargs

    def get_context_data(self, **kwargs):
        from auctions.printing import label_prefs_warnings, warning_matrix

        context = super().get_context_data(**kwargs)
        context["active_tab"] = "printing"
        prefs = self.object
        context["label_prefs"] = prefs
        context["show_print_method"] = self._show_print_method()
        context["show_print_from_computer"] = self._show_print_from_computer()
        # The one fact that decides whether printing to the phone works, rendered next to the
        # checkbox rather than discovered by pressing print.
        device, last_seen = MobileDevice.print_presence_for(self.request.user)
        context["print_phone_device"] = device
        context["print_phone_last_seen"] = last_seen
        context["print_phone_reachable"] = bool(device and device.is_reachable_for_printing)
        # The warnings talk about Bluetooth and thermal printers, which only work in the app.
        show_warnings = bool(self.request.is_mobile_app)
        context["warnings"] = label_prefs_warnings(prefs) if show_warnings else []
        # A plain dict, embedded with |json_script for the live-warning JS.
        context["warning_map"] = warning_matrix() if show_warnings else {}
        userData = self.request.user.userdata
        context["last_auction_used"] = userData.last_auction_used
        context["last_admin_auction"] = (
            Auction.objects.filter(
                Q(created_by=self.request.user) | Q(auctiontos__user=self.request.user, auctiontos__is_admin=True),
                is_deleted=False,
            )
            .order_by("-date_start")
            .first()
        )
        return context


class AccountDeleteView(TemplateView):
    """Delete your account, from the app or the website.

    Required by both app stores (App Store Review 5.1.1(v)) and doable without emailing support. A web
    page because account lifecycle is server logic, and the app already renders /preferences/.

    Confirmation is typing the username, which works for accounts with no password and can't be done by
    accident. The request is reversible for ``GRACE_PERIOD_DAYS`` by signing in, and the session ends at
    /logout/, which the app intercepts to clear its JWT, profile, cookies and push token.
    """

    template_name = "account_delete.html"

    def get_context_data(self, **kwargs):
        from auctions.account_deletion import GRACE_PERIOD_DAYS, deletion_due_date, deletion_summary

        context = super().get_context_data(**kwargs)
        context["active_tab"] = "delete"
        context["grace_period_days"] = GRACE_PERIOD_DAYS
        context["deletion_due"] = deletion_due_date(self.request.user.userdata)
        context["summary"] = deletion_summary(self.request.user)
        return context

    def post(self, request, *args, **kwargs):
        from django.contrib.auth import logout
        from post_office import mail

        from auctions.account_deletion import cancel_deletion, request_deletion

        if request.POST.get("action") == "cancel":
            if cancel_deletion(request.user):
                messages.success(request, "Your account will not be deleted.")
            return redirect(reverse("preferences"))

        typed = (request.POST.get("confirm_username") or "").strip()
        if typed.casefold() != request.user.username.casefold():
            messages.error(request, "Type your username exactly as it's shown to confirm.")
            return redirect(reverse("account_delete"))

        email = request.user.email
        due = request_deletion(request.user)
        if email:
            # Always email: account correspondence, and the phone is about to be signed out.
            mail.send(
                email,
                subject="Your account is scheduled to be deleted",
                message=(
                    f"You asked us to delete your {Site.objects.get_current().domain} account.\n\n"
                    f"It will be deleted on {due:%B %d, %Y}. If you change your mind before then, "
                    "just sign in again and the deletion is cancelled.\n\n"
                    "If this wasn't you, sign in now to cancel it and change your password."
                ),
            )
        logout(request)
        # The confirmation page is public and the session is gone, so whether anyone was emailed
        # travels in the URL.
        target = f"{reverse('account_deleted')}?emailed=1" if email else reverse("account_deleted")
        # End at /logout/ so the app does a native sign-out; it passes a signed-out visitor through.
        return redirect(f"{reverse('account_logout')}?next={quote(target)}")


class AccountDeletedView(TemplateView):
    """Shown after requesting deletion; public, because the session is gone by the time it loads."""

    template_name = "account_deleted.html"

    def get_context_data(self, **kwargs):
        from auctions.account_deletion import GRACE_PERIOD_DAYS

        context = super().get_context_data(**kwargs)
        context["grace_period_days"] = GRACE_PERIOD_DAYS
        context["emailed"] = self.request.GET.get("emailed") == "1"
        return context


class OwnUserDataUpdate(FormFrictionMixin, SuccessMessageMixin, LoginRequiredMixin, UpdateView):
    """Base for the two pages that edit your own ``UserData``: /preferences/ and /notifications/.

    ``SuccessMessageMixin`` is listed first on purpose: the other way round ``UpdateView.form_valid``
    wins the MRO and the success message never runs.
    """

    model = UserData

    def get_object(self, *args, **kwargs):
        # UserData is auto-created with the user, so this is always the caller's own row.
        return UserData.objects.get(user=self.request.user)

    def get_success_url(self):
        # Back to the page just saved, honouring ``?next=`` for pages that link here for one
        # setting. ``.get()``: indexing it turned any other query parameter into a 500.
        return self.request.GET.get("next") or self.request.path

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class UserPreferencesUpdate(OwnUserDataUpdate):
    template_name = "user_preferences.html"
    success_message = "Preferences saved"
    form_class = ChangeUserPreferencesForm


class UserNotificationsUpdate(OwnUserDataUpdate):
    """/notifications/ -- the emails and push half of the old preferences page.

    Split out because it is the half people look for, and because ``distance_unit`` stayed on
    /preferences/, so the radii here are rendered and read in one unit.
    """

    template_name = "user_notifications.html"
    success_message = "Notification settings saved"
    form_class = ChangeUserNotificationsForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["is_mobile_app"] = bool(getattr(self.request, "is_mobile_app", False))
        return kwargs


class UserLocationUpdate(UpdateView, SuccessMessageMixin):
    template_name = "user_location.html"
    model = UserData
    success_message = "Contact info updated"
    form_class = UserLocation
    # UserData and User don't share pks, so /users/1/edit would load the wrong UserData; dispatch
    # sets self.user_pk for get_object.
    user_pk = None

    def dispatch(self, request, *args, **kwargs):
        # self.user_pk = kwargs['pk'] # set the hack
        self.user_pk = request.user.pk
        auth = False
        if self.get_object().user.pk == request.user.pk:
            auth = True
        if request.user.is_superuser:
            auth = True
        if not auth:
            messages.error(request, "Your account doesn't have permission to view this page.")
            return redirect(reverse("home"))
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        data = self.request.GET.copy()
        if len(data) == 0:
            data["next"] = reverse("userpage", kwargs={"slug": self.request.user.username})
            # "/users/" + str(self.kwargs['pk'])
        return data["next"]

    def get_object(self, *args, **kwargs):
        return UserData.objects.get(user__pk=self.user_pk)  # get the hack

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Only the auction gate sets this, until they save once. See services.CONTACT_GATE_NEEDS_PHONE.
        kwargs["require_phone"] = bool(self.request.session.get(CONTACT_GATE_NEEDS_PHONE))
        return kwargs

    def get_initial(self):
        user = User.objects.get(pk=self.get_object().user.pk)
        return {"first_name": user.first_name, "last_name": user.last_name}

    def get_recent_auctiontos(self):
        """The participant rows this page's changes will follow into. See services."""
        from auctions.services import recent_auctiontos_for

        return recent_auctiontos_for(self.request.user)

    def form_valid(self, form):
        from auctions.services import propagate_contact_info

        userData = form.save(commit=False)
        user = User.objects.get(pk=self.get_object().user.pk)
        user.first_name = form.cleaned_data["first_name"]
        user.last_name = form.cleaned_data["last_name"]
        user.save()
        userData.last_activity = timezone.now()
        userData.save()
        # The auctions and clubs holding their own copy of these details, shared with the
        # assistant's update_contact_info.
        propagate_contact_info(user, userData)
        # They've answered the gate's question; leaving the flag set would require a phone number
        # for ever.
        self.request.session.pop(CONTACT_GATE_NEEDS_PHONE, None)
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "contact"

        # Add message about auctions that will be updated
        recent_auctiontos = self.get_recent_auctiontos()
        count = recent_auctiontos.count()
        if count == 1:
            tos = recent_auctiontos.first()
            context["auctiontos_update_message"] = f"Updating your contact info will also update it in {tos.auction}"
        elif count > 1:
            context["auctiontos_update_message"] = f"Updating your contact info will also update it in {count} auctions"

        club_memberships = ClubMember.objects.filter(user=self.request.user, is_deleted=False).select_related("club")
        club_count = club_memberships.count()
        if club_count == 1:
            club = club_memberships.first().club
            context["club_membership_message"] = (
                f"Updating your contact info will also update your contact info in the {club.name}"
            )
        elif club_count > 1:
            context["club_membership_message"] = (
                f"Updating your contact info will also update your contact info in {club_count} clubs"
            )

        return context


class UserChartView(APIView):
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied()
        user = kwargs.get("pk", None)
        allBids = (
            Bid.objects.exclude(is_deleted=True)
            .select_related("lot_number__species_category")
            .filter(user=user, lot_number__species_category__isnull=False)
        )
        pageViews = PageView.objects.select_related("lot_number__species_category").filter(
            user=user, lot_number__species_category__isnull=False
        )
        # Inefficient, but only run for admins and async of page load. Most of it could be a join
        # and a count, but the attributes and sorting keep changing.

        categories = {}
        for item in allBids:
            category = str(item.lot_number.species_category)
            categories.setdefault(category, {"bids": 0, "views": 0})["bids"] += 1
        for item in pageViews:
            category = str(item.lot_number.species_category)
            categories.setdefault(category, {"bids": 0, "views": 0})["views"] += 1
        # sort the result
        sortedCategories = sorted(categories, key=lambda t: -categories[t]["views"])
        # Format for chart.js.
        labels = []
        bids = []
        views = []
        for item in sortedCategories:
            labels.append(item)
            bids.append(categories[item]["bids"])
            views.append(categories[item]["views"])
        return JsonResponse(data={"labels": labels, "bids": bids, "views": views})


class LotChartView(APIView):
    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied()
        lot_number = kwargs.get("pk", None)
        queryset = (
            PageView.objects.filter(lot_number=lot_number)
            .exclude(user_id__isnull=True)
            .order_by("-total_time")
            .values("user_id", "total_time")[:20]
        )
        user_ids = [entry["user_id"] for entry in queryset]
        users_by_id = User.objects.in_bulk(user_ids)
        labels = []
        data = []
        for entry in queryset:
            labels.append(str(users_by_id.get(entry["user_id"], entry["user_id"])))
            data.append(int(entry["total_time"]))

        return JsonResponse(
            data={
                "labels": labels,
                "data": data,
            }
        )


class IgnoreCategoriesView(TemplateView):
    template_name = "ignore_categories.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = "ignore"
        return context


class CreateUserIgnoreCategory(APIView):
    """Add category with given pk to ignore list"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to ignore categories")
            return redirect(reverse("home"))
        pk = self.kwargs.get("pk", None)
        category = Category.objects.get(pk=pk)
        result, created = UserIgnoreCategory.objects.update_or_create(category=category, user=request.user)
        return JsonResponse(data={"pk": result.pk})


class DeleteUserIgnoreCategory(APIView):
    """Allow users to see lots in a given category again."""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        if not self.request.user.is_authenticated:
            messages.error(request, "Sign in to show categories")
            return redirect(reverse("home"))
        pk = self.kwargs.get("pk", None)
        category = Category.objects.get(pk=pk)
        try:
            exists = UserIgnoreCategory.objects.get(category=category, user=request.user)
            exists.delete()
            return JsonResponse(data={"result": "deleted"})
        except UserIgnoreCategory.DoesNotExist:
            return JsonResponse(data={"error": "Category was not ignored."}, status=404)
        except Exception:
            logger.exception("Failed deleting ignored category for user %s", request.user.pk)
            return JsonResponse(data={"error": "Unable to update ignored categories."}, status=500)


class GetUserIgnoreCategory(APIView):
    """Get a list of all user ignore categories for the request user"""

    authentication_classes = [SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        categories = Category.objects.all().order_by("name")
        results = []
        for category in categories:
            item = {
                "id": category.pk,
                "text": category.name,
            }
            try:
                UserIgnoreCategory.objects.get(user=request.user, category=category.pk)
                item["selected"] = True
            except UserIgnoreCategory.DoesNotExist:
                pass
            results.append(item)
        return JsonResponse({"results": results}, safe=False)
