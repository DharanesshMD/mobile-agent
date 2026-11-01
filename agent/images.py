"""Pictures the owner shares in chat: checked, shrunk, stripped of metadata, and turned into the blocks the model API wants.

Meta's Chat Completions takes {"type":"image_url","image_url":{"url":"data:image/<type>;base64,..."}} blocks, in user messages only (JPEG, PNG, GIF, WEBP; 50 MB
and 50 images per request at most). Cost grows with resolution (about 1,300-1,500 tokens for a 1280 px picture), so what is sent to the model is capped at 1280 px.
"""
import base64
import io

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # raw size of one upload
MAX_PIXELS = 60_000_000  # refuse decompression bombs before decoding them
STORE_EDGE = 1568  # the longest edge of what is kept on disk
MODEL_EDGE = 1280  # the longest edge of what is sent to the model
MAX_PER_MESSAGE = 4
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}  # sniffed from the bytes, never from the file name
EXT = {"image/jpeg": "jpg", "image/png": "png"}  # what is stored is always one of these two
IMAGE_ONLY = "\U0001F4F7"  # the text of a message that is only a picture (a camera): the chat list shows it, the web page hides it, the model is told what it means
FLAT_COLORS = 1500  # a screenshot or a drawing has few distinct colors; a photograph has thousands
PNG_LIMIT = 4 * 1024 * 1024  # a flat picture that still comes out larger than this is kept as JPEG instead


class ImageError(ValueError):
    """The upload is not a picture we accept; the message is shown to the owner."""


def _open(data: bytes) -> Image.Image:
    if not data:
        raise ImageError("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(f"That picture is too big ({len(data) // 1024 // 1024} MB). The limit is {MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if fmt not in FORMATS:
            raise ImageError("Only JPEG, PNG, GIF and WEBP pictures are supported.")
        if img.width * img.height > MAX_PIXELS:
            raise ImageError("That picture has too many pixels. Send a smaller one.")
        img.load()
        return img
    except ImageError:
        raise
    except Exception as e:  # corrupt, truncated, or not a picture at all
        raise ImageError("That file is not a picture I can read.") from e


def _clean(img: Image.Image) -> Image.Image:
    """The same pixels in a fresh image: nothing from the file's EXIF, GPS, ICC, text chunks or comments can come along."""
    return Image.frombytes(img.mode, img.size, img.tobytes())


def _flat(img: Image.Image) -> bool:
    small = img.convert("RGB")
    small.thumbnail((96, 96), Image.NEAREST)
    colors = small.getcolors(maxcolors=96 * 96)
    return colors is not None and len(colors) <= FLAT_COLORS


def _encode(img: Image.Image, png: bool) -> tuple[bytes, str]:
    out = io.BytesIO()
    if png:
        img.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    img.convert("RGB").save(out, "JPEG", quality=85, optimize=True)
    return out.getvalue(), "image/jpeg"


def normalize(data: bytes) -> tuple[bytes, str, int, int]:
    """Check a picture and return (bytes, mime, width, height) as it is stored: orientation applied, longest edge at most 1568 px, JPEG or PNG
    (PNG for transparency and for screenshots or drawings, where JPEG would blur the text), and no metadata at all."""
    img = _open(data)
    img = ImageOps.exif_transpose(img)  # a phone photo's rotation lives in EXIF: apply it before the metadata is dropped
    alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    img = img.convert("RGBA" if alpha else "RGB")
    img.thumbnail((STORE_EDGE, STORE_EDGE), Image.LANCZOS)
    img = _clean(img)
    png = alpha or _flat(img)
    out, mime = _encode(img, png)
    if png and not alpha and len(out) > PNG_LIMIT:
        out, mime = _encode(img, False)
    return out, mime, img.width, img.height


def for_model(data: bytes, mime: str) -> str:
    """A stored picture as a data URL for the model, shrunk to MODEL_EDGE on its longest side."""
    img = Image.open(io.BytesIO(data))
    if max(img.size) > MODEL_EDGE:
        img.load()
        img.thumbnail((MODEL_EDGE, MODEL_EDGE), Image.LANCZOS)
        data, mime = _encode(_clean(img.convert("RGBA" if img.mode == "RGBA" else "RGB")), mime == "image/png")
    return f"data:{mime};base64," + base64.b64encode(data).decode()

