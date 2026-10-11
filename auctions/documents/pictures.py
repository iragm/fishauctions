"""Pictures on a document: the photographs and drawings an article printed, cropped out and kept.

They arrive through a signed link rather than as a tool argument. An assistant transcribing a scan sees
the picture but can't put its bytes into a tool call, so ``add_document`` hands back the link and the
assistant's own script posts the crops to it (``curl -F file=@fish.jpg -F caption=... <link>``), or the
person opens it and picks them on their phone. The link is the credential, for one document, so the
view is CSRF-exempt and needs no session; it stops working when its signer can no longer change the
document.
"""

import hashlib
import io

from django.core import signing
from django.db.models import Max
from django.urls import reverse

from auctions.documents.models import DocumentPicture

#: How long a link keeps working: long enough for a person to come back to it the next evening.
LINK_DAYS = 7
MAX_PICTURES_PER_DOCUMENT = 40
#: Per picture as sent. Under nginx's 20M for the whole request; stored copies are far smaller.
MAX_PICTURE_BYTES = 15 * 1024 * 1024
#: Longest side kept. Bigger than a page scan's, since a picture is the one thing a reader zooms into.
MAX_SIDE = 3000

_SALT = "library-pictures"


def upload_link(document, user) -> str:
    """The path of a link that lets ``user``'s say-so add pictures to ``document`` for :data:`LINK_DAYS`."""
    token = signing.dumps([document.pk, user.pk], salt=_SALT, compress=True)
    return reverse("document_pictures", args=[document.pk, token])


def signer_of(document_pk: int, token: str):
    """The account a link was signed for, if it is for this document and hasn't expired; else ``None``."""
    from django.contrib.auth import get_user_model

    try:
        signed_document, user_pk = signing.loads(token, salt=_SALT, max_age=LINK_DAYS * 24 * 60 * 60)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if signed_document != document_pk:
        return None
    return get_user_model().objects.filter(pk=user_pk, is_active=True).select_related("userdata").first()


def _cleaned(data: bytes) -> tuple[bytes, str] | tuple[None, str]:
    """The picture re-encoded, upright, bounded and without its metadata (a phone's GPS position
    included), and its extension; or ``None`` and why not. Drawings stay PNG so their lines stay sharp.
    """
    from PIL import Image, ImageOps

    try:
        import HeifImagePlugin  # noqa: F401
    except ImportError:
        pass
    try:
        with Image.open(io.BytesIO(data)) as image:
            lossless = image.format in {"PNG", "GIF", "BMP", "TIFF"}
            picture = ImageOps.exif_transpose(image)
            picture.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            if lossless:
                picture.convert("RGBA" if "A" in picture.getbands() else "RGB").save(out, "PNG", optimize=True)
                return out.getvalue(), ".png"
            picture.convert("RGB").save(out, "JPEG", quality=88)
            return out.getvalue(), ".jpg"
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        return None, "That isn't a picture this site can open. Send a JPEG, PNG, WebP, GIF or HEIC."


def add_pictures(document, uploads, captions) -> tuple[list[DocumentPicture], list[str]]:
    """Save each upload as a picture on ``document``, in order. Returns what was added and, for each
    one that wasn't, why. A picture already on the document is skipped, not added twice.
    """
    from django.core.files.base import ContentFile

    from auctions.documents.forms import MAX_BYTES_PER_PERSON, space_used

    added, problems = [], []
    count = document.pictures.count()
    position = (document.pictures.aggregate(last=Max("position"))["last"] or 0) + 1
    used = space_used(document.owner) if document.owner_id else 0
    for index, upload in enumerate(uploads):
        name = upload.name or f"picture {index + 1}"
        if count >= MAX_PICTURES_PER_DOCUMENT:
            problems.append(f"{name}: a document can have up to {MAX_PICTURES_PER_DOCUMENT} pictures.")
            continue
        if upload.size > MAX_PICTURE_BYTES:
            problems.append(f"{name}: pictures can be up to {MAX_PICTURE_BYTES // (1024 * 1024)} MB.")
            continue
        data, extension = _cleaned(upload.read())
        if data is None:
            problems.append(f"{name}: {extension}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        if document.pictures.filter(sha256=digest).exists():
            problems.append(f"{name}: already on this document.")
            continue
        if used + len(data) > MAX_BYTES_PER_PERSON:
            problems.append(f"{name}: that's more than the library space. Delete something first.")
            continue
        caption = str(captions[index] if index < len(captions) else "").strip()[:300]
        picture = DocumentPicture(document=document, caption=caption, position=position, size=len(data), sha256=digest)
        picture.file.save(f"picture{extension}", ContentFile(data), save=False)
        picture.save()
        added.append(picture)
        count += 1
        position += 1
        used += len(data)
    return added, problems
