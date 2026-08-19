from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

from app.schemas import ValidatedPanel

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DECODED_PIXELS = 40_000_000
SUPPORTED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png"}


class UploadValidationError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def validate_image(
    content: bytes,
    panel_id: str,
) -> tuple[ValidatedPanel, bool]:
    if not content:
        raise UploadValidationError(400, "empty_upload", "The uploaded panel is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise UploadValidationError(
            413,
            "upload_too_large",
            f"The uploaded panel exceeds {MAX_UPLOAD_BYTES} bytes.",
        )

    try:
        with Image.open(io.BytesIO(content)) as source:
            image_format = source.format
            if image_format not in SUPPORTED_FORMATS:
                raise UploadValidationError(
                    415,
                    "unsupported_image_type",
                    "Only decoded JPEG and PNG images are accepted.",
                )

            exif_orientation = source.getexif().get(274)
            source_width, source_height = source.size
            if source_width * source_height > MAX_DECODED_PIXELS:
                raise UploadValidationError(
                    413,
                    "too_many_pixels",
                    f"The decoded panel exceeds {MAX_DECODED_PIXELS} pixels.",
                )

            oriented = ImageOps.exif_transpose(source)
            oriented.load()
            width, height = oriented.size
            if width <= 0 or height <= 0:
                raise UploadValidationError(
                    400,
                    "invalid_dimensions",
                    "The decoded panel has invalid dimensions.",
                )

            grayscale = oriented.convert("L")
            variance = ImageStat.Stat(grayscale).var[0]
    except UploadValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UploadValidationError(
            415,
            "invalid_image",
            "The upload could not be decoded as a valid JPEG or PNG image.",
        ) from exc

    applied_transform = (
        "exif_transpose" if exif_orientation not in (None, 1) else "none"
    )
    panel = ValidatedPanel(
        panel_id=panel_id,
        source_sha256=hashlib.sha256(content).hexdigest(),
        detected_mime_type=SUPPORTED_FORMATS[image_format],
        width=width,
        height=height,
        exif_orientation=exif_orientation,
        applied_transform=applied_transform,
        decoded_pixel_count=width * height,
        original_byte_count=len(content),
        preprocessing_version="decoded-original-v1",
    )
    return panel, variance < 1.0
