"""The two forms behind the report button and the copyright notice page.

They are here rather than in ``forms.py`` for the same reason
:mod:`auctions.moderation_models` is not in ``models.py``: that file is at the ceiling
``auctions/module_map.py`` holds it to, and the ratchet only comes down.

:class:`CopyrightNoticeForm` is deliberately not the contact form with a different subject line.
A notice under 17 U.S.C. 512(c)(3)(A) has six required parts, two of which are statements the
sender has to actually make -- a good-faith belief, and an accuracy statement under penalty of
perjury -- and a free-text box collects none of them. It matters in both directions: a notice
missing pieces does not start our removal clock (512(c)(3)(B)), and a sender who ticks the perjury
box has been shown, in the same breath, that 512(f) makes a knowingly false notice actionable.
That warning is the cheapest defence there is against the bogus notice used to take down a rival's
listing.

Neither form is the only way in. The address published in the Copyright Office directory is the
address that legally counts, and a notice emailed there is valid whether or not it came through
here -- see :mod:`auctions.dmca`.
"""

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit
from django import forms
from django_recaptcha.fields import ReCaptchaField
from django_recaptcha.widgets import ReCaptchaV2Invisible

from auctions.forms import add_bootstrap_classes, recaptcha_is_configured
from auctions.moderation_models import ContentReport, CopyrightNotice


class _CaptchaMixin:
    """Drop the captcha when the site has no keys, exactly as the contact form does.

    Without this every local and CI run would have to solve one.
    """

    def _drop_captcha_if_unconfigured(self):
        if not recaptcha_is_configured():
            self.fields.pop("captcha", None)


class ReportContentForm(_CaptchaMixin, forms.ModelForm):
    """The report button on a lot: anything that is not a copyright claim.

    Open to people who are not signed in, because the person best placed to notice that a listing
    is a scam is often somebody who has not made an account. Signed in, the two contact fields are
    dropped rather than prefilled -- the account already says who this is, and a posted value would
    not be trusted anyway.
    """

    reporter_name = forms.CharField(max_length=100, label="Your name", required=False)
    reporter_email = forms.EmailField(
        max_length=254,
        label="Your email",
        required=False,
        help_text="Optional, but we can't ask you anything without it.",
    )
    captcha = ReCaptchaField(widget=ReCaptchaV2Invisible)

    class Meta:
        model = ContentReport
        fields = ["reason", "details"]
        labels = {
            "reason": "What's wrong with it?",
            "details": "Anything else we should know?",
        }
        widgets = {"details": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self._drop_captcha_if_unconfigured()
        self.fields["details"].required = False
        if self.user is not None and self.user.is_authenticated:
            self.fields.pop("reporter_name", None)
            self.fields.pop("reporter_email", None)
        add_bootstrap_classes(self)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.add_input(Submit("submit", "Send report", css_class="btn-success text-dark"))


class CopyrightNoticeForm(_CaptchaMixin, forms.ModelForm):
    """A DMCA notice, with all six of the things 512(c)(3)(A) asks a notice to contain.

    ``good_faith`` and ``accurate`` are the two statements, and both are required: a form that let
    somebody through without them would be collecting something that is not a notice while looking
    like it collects notices.
    """

    captcha = ReCaptchaField(widget=ReCaptchaV2Invisible)

    class Meta:
        model = CopyrightNotice
        fields = [
            "name",
            "email",
            "phone",
            "address",
            "on_behalf_of",
            "work",
            "material",
            "good_faith",
            "accurate",
            "signature",
        ]
        labels = {
            "name": "Your full name",
            "email": "Your email address",
            "phone": "Your phone number",
            "address": "Your mailing address",
            "on_behalf_of": "Copyright owner, if you are not the owner yourself",
            "work": "What work of yours is being infringed?",
            "material": "What on this site infringes it, and where is it?",
            "good_faith": (
                "I have a good faith belief that the use of the material described above is not "
                "authorised by the copyright owner, its agent, or the law."
            ),
            "accurate": (
                "I state under penalty of perjury that the information in this notice is accurate, "
                "and that I am the copyright owner or am authorised to act on the owner's behalf."
            ),
            "signature": "Type your full legal name as your signature",
        }
        help_texts = {
            "work": "For example: a photograph of a particular fish, first published at this address.",
            "material": (
                "Paste the full web address of every page or image you are complaining about. "
                "We can only remove material we can find."
            ),
            "phone": "Required. A notice without a way to reach you is not a complete notice.",
        }
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "work": forms.Textarea(attrs={"rows": 4}),
            "material": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self._drop_captcha_if_unconfigured()
        for name in ("phone", "signature", "address", "work", "material"):
            self.fields[name].required = True
        add_bootstrap_classes(self)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.add_input(Submit("submit", "Send this notice", css_class="btn-success text-dark"))

    def clean_good_faith(self):
        return self._require_statement("good_faith", "You have to make this statement for this to be a valid notice.")

    def clean_accurate(self):
        return self._require_statement("accurate", "You have to make this statement for this to be a valid notice.")

    def _require_statement(self, field, message):
        value = self.cleaned_data.get(field)
        if not value:
            raise forms.ValidationError(message)
        return value
