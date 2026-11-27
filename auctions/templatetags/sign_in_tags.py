"""The email box on the one sign-in page (``account/partials/sign_in_choices.html``)."""

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def login_code_form(context):
    """An unbound code request form, pre-filled with an address the page already knows."""
    from auctions.login_code import CustomRequestLoginCodeForm

    request = context.get("request")
    email = ""
    form = context.get("form")
    if form is not None and getattr(form, "is_bound", False):
        email = form.data.get("email") or form.data.get("login") or ""
    if request is not None and not email:
        email = request.GET.get("email", "")
    if email and "@" not in email:
        email = ""
    return CustomRequestLoginCodeForm(initial={"email": email})
