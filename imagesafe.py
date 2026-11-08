"""Decode limits for every image we fetch or accept (H-1, M2 re-audit).

A PNG or WebP can be a few kilobytes on the wire and 10,000 x 10,000 pixels
once decoded (a decompression bomb): a 32 KB file took 700 MB to decode. The
app is one process with many threads, so a few of those at once can stop it.
So every image goes through here before anything decodes its pixels:

  * a byte cap (what the fetcher or the upload allows, checked again here);
  * a pixel cap, read from the header only (Image.open reads the size without
    decoding), before any load, convert or composite;
  * Pillow's own guard (Image.MAX_IMAGE_PIXELS, set once on import): anything
    that skips this module still can't decode more than 2x MAX_PIXELS;
  * thumbnails use draft mode (JPEG decodes at 1/2, 1/4 or 1/8 scale) and
    shrink before any convert, so a big photo never becomes a full-size RGBA
    copy in memory.

Callers: product_import.download_image (photos a page import found),
service_path.download_logo and image_colors (the site's logo), core._check_photo
(uploads), creator.photo_for_writer, presenter_stills, exporter.
"""
import io
import warnings

from PIL import Image

MAX_PIXELS = 50_000_000            # uploads (core.MAX_PHOTO_PIXELS): about 8K x 6K
FETCH_MAX_PIXELS = 25_000_000      # a photo fetched from a page: 5000 x 5000
LOGO_MAX_PIXELS = 4096 * 4096      # a logo
MAX_BYTES = 15 * 1024 * 1024       # what we fetch (product_import.MAX_IMAGE_BYTES)

# Pillow refuses (DecompressionBombError) above 2x this and warns above it; we turn the warning into a refusal below.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class BadImage(ValueError):
    """Not an image we decode (wrong type, too big, broken). The message is safe to show."""


def _open(src):
    """Open lazily (header only). `src`: bytes or a file path."""
    return Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)


def probe(src, max_pixels: int = FETCH_MAX_PIXELS, max_bytes: int | None = MAX_BYTES) -> tuple[str, int, int]:
    """(content type, width, height) of a JPEG, PNG or WebP, read from its header
    only: no pixels are decoded. Raises BadImage when it's another type, over
    max_bytes, or over max_pixels."""
    if isinstance(src, (bytes, bytearray)) and max_bytes is not None and len(src) > max_bytes:
        raise BadImage("That image is too large.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with _open(src) as im:
                fmt, (w, h) = (im.format or "").upper(), im.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise BadImage("That image is too large in pixels.") from None
    except Exception:  # noqa: BLE001  Pillow raises many kinds for broken files
        raise BadImage("That isn't an image we can use.") from None
    if fmt not in TYPES:
        raise BadImage("Only JPEG, PNG or WebP images.")
    if w <= 0 or h <= 0 or w * h > max_pixels:
        raise BadImage("That image is too large in pixels.")
    return TYPES[fmt], w, h


def thumbnail(src, size: tuple[int, int], mode: str = "RGB", max_pixels: int = MAX_PIXELS):
    """A decoded copy of the image no bigger than `size`, in `mode`. Checks the
    pixel cap first, decodes JPEGs in draft mode (reduced scale) and shrinks
    before converting. Raises BadImage."""
    probe(src, max_pixels=max_pixels, max_bytes=None)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = _open(src)
            if (im.format or "").upper() == "JPEG":
                im.draft("RGB", size)            # decode at 1/2, 1/4 or 1/8 scale when that still covers `size`
            im.thumbnail(size)                   # loads (at the draft size) and shrinks in place
            out = im.convert(mode) if im.mode != mode else im.copy()
            im.close()
            return out
    except BadImage:
        raise
    except Exception:  # noqa: BLE001
        raise BadImage("That isn't an image we can use.") from None
