"""Single-lot label PDF for the mobile ``fishauctions://print/<pk>`` deep link.

Reuses the web ``SingleLotLabelView`` WeasyPrint pipeline verbatim so a lot printed from the app
(when ``print_method`` is ``pdf``/``system``) has the exact same layout — and honours the same
``UserLabelPrefs`` — as one printed from the website.
"""

import logging

logger = logging.getLogger(__name__)


def render_single_lot_pdf(lot, request):
    """Render *lot*'s label as a one-lot PDF using the caller's saved label prefs.

    ``request`` is the DRF request (its ``user`` is the JWT-authenticated user). Returns
    ``(pdf_bytes, "application/pdf")``. Raises ``ValueError`` if the lot has no auction to render
    against (mirrors the web view, which drives labels off the auction's print-field config).
    """
    from auctions.views import SingleLotLabelView

    auction = lot.auction or (lot.auctiontos_seller.auction if lot.auctiontos_seller else None)
    if auction is None:
        msg = "Lot has no auction; cannot render a label PDF."
        raise ValueError(msg)

    # The CBV reads self.request.user for the prefs and self.request for the PDF base URL. Use the
    # underlying Django request but pin the JWT user onto it.
    django_request = getattr(request, "_request", request)
    django_request.user = request.user

    view = SingleLotLabelView()
    view.request = django_request
    view.args = ()
    view.kwargs = {}
    view.lot = lot
    view.auction = auction

    context = view.get_context_data()
    response = view.render_to_response(context)
    response.render()
    return bytes(response.content), "application/pdf"
