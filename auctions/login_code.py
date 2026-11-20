"""Sign in with an emailed code: no password, no username, and the code is the verification.

allauth's own login-by-code (``ACCOUNT_LOGIN_BY_CODE_ENABLED``) signs in an account that already
exists. This module adds the one thing it won't do, and closes two holes it leaves open here:

- **An address with no account gets a code too.** The account is created only when the right code
  comes back, never when it is asked for, so a stranger typing addresses creates nothing.
  ``models.account_for_email`` links rows to any active account with no verification check, so an
  account made at request time would collect somebody's invoices before they proved the inbox.
  Signing in then runs ``signals.on_sign_in`` like any other route, which claims every AuctionTOS
  and ClubMember row a club wrote for that address.
- **A squatter's password dies.** Anyone can sign up with somebody else's address and a password;
  the account waits, unverified and unusable. A code sign-in verifies that address, which would make
  the stranger's password work on the real owner's account, so it is made unusable first (the app's
  Google sign-in does the same, ``mobile/views.py``).
- **An unverified second address doesn't count.** allauth would sign into an account that merely
  listed the address, unverified, beside its own. Here such an address belongs to nobody yet.

Accounts made here start with no promotional mail; the one box on the code page turns on news of
auctions near them. Nothing is sent to an address the site has marked as bouncing.

The views replace allauth's at the same two paths and names (``fishauctions/urls.py`` lists them
first), so every ``{% url %}`` and allauth redirect lands here.
"""

from allauth.account.adapter import get_adapter
from allauth.account.forms import RequestLoginCodeForm
from allauth.account.internal.decorators import login_stage_required
from allauth.account.internal.flows.login_by_code import LoginCodeVerificationProcess
from allauth.account.internal.userkit import user_id_to_str
from allauth.account.models import EmailAddress, Login
from allauth.account.stages import LoginByCodeStage, LoginStageController
from allauth.account.views import ConfirmLoginCodeView, RequestLoginCodeView
from django import forms
from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from django_recaptcha.fields import ReCaptchaField
from django_recaptcha.widgets import ReCaptchaV2Invisible

NEW_ACCOUNT_KEY = "fa_new_account"
WANTS_AUCTION_EMAIL_KEY = "fa_wants_auction_email"
PROMO_FLAGS = (
    "email_me_about_new_auctions",
    "email_me_about_new_in_person_auctions",
    "email_me_about_new_features",
    "email_me_about_new_local_lots",
    "email_me_about_new_lots_ship_to_location",
)
NEARBY_AUCTION_FLAGS = ("email_me_about_new_auctions", "email_me_about_new_in_person_auctions")


def _address_bounces(email):
    """True when the site marked *email* bad and never saw it work since."""
    from auctions.models import AuctionTOS, ClubMember

    statuses = set(AuctionTOS.objects.filter(email__iexact=email).values_list("email_address_status", flat=True))
    statuses |= set(
        ClubMember.objects.filter(email__iexact=email, is_deleted=False).values_list("email_address_status", flat=True)
    )
    return "BAD" in statuses and "VALID" not in statuses


def address_is_free(email):
    """No account, active or not, has *email* as its own or as a verified extra address."""
    if User.objects.filter(email__iexact=email).exists():
        return False
    return not EmailAddress.objects.filter(email__iexact=email, verified=True).exists()


def counts_for(user, email):
    """Whether *email* signs in to *user*: their own address, or an extra one they verified."""
    if (user.email or "").lower() == email.lower():
        return True
    return EmailAddress.objects.filter(user=user, email__iexact=email, verified=True).exists()


def _name_from_club_rows(email):
    """The newest name a club typed for *email*, as (first, last)."""
    from auctions.models import AuctionTOS, ClubMember

    candidates = []
    tos = (
        AuctionTOS.objects.filter(email__iexact=email, user__isnull=True)
        .exclude(name__isnull=True)
        .exclude(name="")
        .order_by("-createdon")
        .first()
    )
    if tos:
        candidates.append((tos.createdon, tos.name))
    member = (
        ClubMember.objects.filter(email__iexact=email, user__isnull=True, is_deleted=False)
        .exclude(name="")
        .order_by("-createdon")
        .first()
    )
    if member:
        candidates.append((member.createdon, member.name))
    if not candidates:
        return "", ""
    name = max(candidates, key=lambda c: c[0])[1].strip()
    first, _, last = name.partition(" ")
    return first[:30], last.strip()[:150]


@transaction.atomic
def create_account(request, email, wants_auction_email=False):
    """A new account for *email*, verified, passwordless, with promotional mail off."""
    adapter = get_adapter(request)
    user = adapter.new_user(request)
    user.email = email
    user.first_name, user.last_name = _name_from_club_rows(email)
    adapter.populate_username(request, user)
    user.set_unusable_password()
    user.save()
    # An unverified copy of the address on someone else's account is a squatter's; it goes.
    EmailAddress.objects.filter(email__iexact=email, verified=False).exclude(user=user).delete()
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    userdata = user.userdata
    for flag in PROMO_FLAGS:
        setattr(userdata, flag, False)
    if wants_auction_email:
        for flag in NEARBY_AUCTION_FLAGS:
            setattr(userdata, flag, True)
    userdata.save(update_fields=list(PROMO_FLAGS))
    return user


def prepare_existing_account(user, email):
    """Before a code verifies *email* on *user*: kill a password nobody proved, and make sure allauth
    has an address row to verify (older accounts can lack one)."""
    address = EmailAddress.objects.filter(user=user, email__iexact=email).first()
    if address is None:
        address = EmailAddress.objects.create(
            user=user, email=email, primary=not user.emailaddress_set.filter(primary=True).exists()
        )
    if not address.verified and user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=["password"])


class CodeSignInProcess(LoginCodeVerificationProcess):
    @classmethod
    def initiate(cls, *, request, user, email=None, phone=None, stage=None):
        # allauth's initiate and resume name their own class; these name ours.
        initial_state = cls.initial_state(user=user, email=email, phone=phone)
        initial_state["initiated_by_user"] = stage is None
        if not stage:
            login = Login(user=user, email=email)
            login.state["stages"] = {"current": "login_by_code"}
            stage = LoginByCodeStage(LoginStageController(request, login), request, login)
        stage.state.update(initial_state)
        process = cls(stage=stage)
        process.send()
        process.persist()
        return process

    @classmethod
    def resume(cls, stage):
        return cls(stage=stage).abort_if_invalid()

    def send_by_email(self, email, skip_enumeration_mails=False):
        if self.user is not None or self.stage.login.signup:
            return super().send_by_email(email, skip_enumeration_mails=skip_enumeration_mails)
        if address_is_free(email) and not _address_bounces(email):
            self.state[NEW_ACCOUNT_KEY] = True
            code = self.generate_code()
            get_adapter().send_mail("account/email/login_code", email, {"request": self.request, "code": code})
        # Same page either way: nobody learns which addresses have accounts.
        self.add_sent_message({"email": email, "recipient": email})

    def finish(self, redirect_url):
        email = self.state.get("email")
        user = self.user
        if user is None:
            if not (email and self.state.get(NEW_ACCOUNT_KEY)):
                self.abort()
                return HttpResponseRedirect(reverse("account_request_login_code"))
            wants = bool(self.state.get(WANTS_AUCTION_EMAIL_KEY))
            if address_is_free(email):
                user = create_account(self.request, email, wants_auction_email=wants)
            else:
                # Someone made an account with this address while the code was out.
                user = next((u for u in _active_users_for(email) if counts_for(u, email)), None)
                if user is None:
                    self.abort()
                    return HttpResponseRedirect(reverse("account_request_login_code"))
            self._user = user
            self.state["user_id"] = user_id_to_str(user)
        else:
            wants = bool(self.state.get(WANTS_AUCTION_EMAIL_KEY))
            if wants:
                _turn_on_nearby_auction_email(user)
        prepare_existing_account(user, email)
        response = super().finish(redirect_url)
        from auctions.services import clear_email_bounce

        clear_email_bounce(user)
        return response


def _active_users_for(email):
    from allauth.account.internal.userkit import filter_users_by_email

    return filter_users_by_email(email, is_active=True, prefer_verified=True, for_login=True)


def _turn_on_nearby_auction_email(user):
    from auctions.models import UserData

    UserData.objects.filter(user=user).update(**dict.fromkeys(NEARBY_AUCTION_FLAGS, True))


class CustomRequestLoginCodeForm(RequestLoginCodeForm):
    captcha = ReCaptchaField(widget=ReCaptchaV2Invisible)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from auctions.forms import recaptcha_is_configured

        if not recaptcha_is_configured():
            self.fields.pop("captcha", None)
        self.fields["email"].label = "Email"
        self.fields["email"].widget.attrs.update({"autocomplete": "email", "placeholder": ""})

    def clean_email(self):
        email = super().clean_email()
        user = getattr(self, "_user", None)
        if email and user is not None and not counts_for(user, email):
            # Listed, unverified, beside another address: not this person's account.
            self._user = None
        return email


class CodeConfirmForm(forms.Form):
    """Only what the confirm page adds to allauth's: the one opt-in box."""

    email_me_about_auctions = forms.BooleanField(required=False, label="Email me about auctions near me")


class CodeRequestView(RequestLoginCodeView):
    def get_initial(self):
        initial = super().get_initial()
        uuid = self.request.GET.get("invoice")
        if uuid:
            from auctions.models import Invoice

            invoice = Invoice.objects.filter(no_login_link=uuid).select_related("auctiontos_user").first()
            if invoice and invoice.auctiontos_user and invoice.auctiontos_user.email:
                initial["email"] = invoice.auctiontos_user.email
        return initial

    def form_valid(self, form):
        CodeSignInProcess.initiate(
            request=self.request,
            user=form._user,
            email=form.cleaned_data.get("email"),
            phone=form.cleaned_data.get("phone"),
        )
        return HttpResponseRedirect(self.get_success_url())


@method_decorator(
    login_stage_required(stage=LoginByCodeStage.key, redirect_urlname="account_request_login_code"),
    name="dispatch",
)
class CodeConfirmView(ConfirmLoginCodeView):
    @method_decorator(never_cache)
    def dispatch(self, request, *args, **kwargs):
        # allauth's dispatch, with our process in place of its own.
        self.stage = request._login_stage
        self._process = CodeSignInProcess.resume(self.stage)
        if not self._process:
            return HttpResponseRedirect(reverse("account_request_login_code"))
        if request.method == "POST" and request.POST.get("action", "verify") == "verify":
            opt_in = CodeConfirmForm(request.POST)
            self._process.state[WANTS_AUCTION_EMAIL_KEY] = (
                opt_in.is_valid() and opt_in.cleaned_data["email_me_about_auctions"]
            )
        return super(ConfirmLoginCodeView, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["opt_in_form"] = CodeConfirmForm()
        return context


request_login_code = CodeRequestView.as_view()
confirm_login_code = CodeConfirmView.as_view()
