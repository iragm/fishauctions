"""DRF renderers for mobile endpoints that return raw bytes.

``APIView.initial()`` runs content negotiation *before* authentication, against the view's
renderer_classes. With DRF's default set (JSON + browsable API) a client that honestly asks for
what the endpoint actually returns — ``Accept: application/pdf`` or ``Accept: image/png`` — gets
a 406 before the view body ever runs, while ``Accept: */*`` sails through. That produced the
"Could not load the label. Please try again." bug: *all* label fetching failed, PDF and Bluetooth
PNG alike.

The fix is to declare renderers matching what these views really return. The views hand back a
plain ``HttpResponse`` of bytes, so these renderers never render the successful body; they exist
so negotiation succeeds. They still have to cope with DRF *error* payloads (403/404/429 render
through whichever renderer negotiation picked), which is why non-bytes data falls back to JSON.
"""

from rest_framework.renderers import BaseRenderer, JSONRenderer


class BinaryRenderer(BaseRenderer):
    """Pass-through for views that return an HttpResponse of bytes."""

    media_type = "*/*"
    format = "bin"
    charset = None
    render_style = "binary"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        if isinstance(data, bytes):
            return data
        if isinstance(data, str):
            return data.encode("utf-8")
        # A DRF error payload ({"detail": …}) reaching a binary renderer means the client's Accept
        # header allowed only bytes. The app reads `detail`, so serve JSON and correct the
        # Content-Type (Response sets it from the negotiated media type before calling render()).
        response = (renderer_context or {}).get("response")
        if response is not None:
            response["Content-Type"] = "application/json"
        return JSONRenderer().render(data)


class PdfRenderer(BinaryRenderer):
    media_type = "application/pdf"
    format = "pdf"


class PngRenderer(BinaryRenderer):
    media_type = "image/png"
    format = "png"
