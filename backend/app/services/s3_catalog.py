"""Read an explicitly configured public S3 case catalogue safely.

The browser never receives an arbitrary source URL.  This module accepts only
the deployment's `VERIFICATION_S3_CATALOG_URL`, resolves manifest object keys
under that URL's directory, and refuses redirects to another origin.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from app.schemas import SourceCatalog, SourceCatalogCase, SourceObject

CATALOG_ENV: Final = "VERIFICATION_S3_CATALOG_URL"
FETCH_TIMEOUT_SECONDS: Final = 15


class CatalogError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_origin: tuple[str, str, int | None]):
        super().__init__()
        self.allowed_origin = allowed_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if _origin(newurl) != self.allowed_origin:
            raise CatalogError("redirect_not_allowed", "Catalog objects may not redirect to another host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


@dataclass(frozen=True)
class CatalogSource:
    catalog_url: str

    @classmethod
    def configured(cls) -> CatalogSource:
        value = os.getenv(CATALOG_ENV, "").strip()
        if not value:
            raise CatalogError("catalog_not_configured", "Public catalog import is not configured on this service.")
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise CatalogError("invalid_catalog_url", "The configured catalog URL must be an HTTPS URL.")
        if not parsed.path or parsed.path.endswith("/"):
            raise CatalogError("invalid_catalog_url", "The configured catalog URL must identify a manifest file.")
        return cls(catalog_url=value)

    @property
    def origin(self) -> tuple[str, str, int | None]:
        return _origin(self.catalog_url)

    @property
    def object_base_url(self) -> str:
        # Object keys in the manifest are relative to the manifest directory.
        return self.catalog_url.rsplit("/", 1)[0] + "/"

    def object_url(self, key: str) -> str:
        if key.startswith("/") or ".." in key.split("/") or "://" in key:
            raise CatalogError("invalid_object_key", "Catalog object key is not a safe relative path.")
        url = urljoin(self.object_base_url, key)
        if _origin(url) != self.origin or not url.startswith(self.object_base_url):
            raise CatalogError("invalid_object_key", "Catalog object is outside the configured catalog prefix.")
        return url

    def fetch(self, url: str, *, max_bytes: int) -> bytes:
        if _origin(url) != self.origin:
            raise CatalogError("source_not_allowed", "Catalog objects must use the configured catalog host.")
        request = Request(url, headers={"Accept": "application/json, image/png, image/jpeg"})
        opener = build_opener(_SameOriginRedirectHandler(self.origin))
        try:
            with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > max_bytes:
                    raise CatalogError("object_too_large", f"Catalog object exceeds {max_bytes} bytes.")
                content = response.read(max_bytes + 1)
        except CatalogError:
            raise
        except HTTPError as exc:
            raise CatalogError("source_http_error", f"Catalog source returned HTTP {exc.code}.") from exc
        except (URLError, OSError, ValueError) as exc:
            raise CatalogError("source_unavailable", "Catalog source could not be fetched.") from exc
        if len(content) > max_bytes:
            raise CatalogError("object_too_large", f"Catalog object exceeds {max_bytes} bytes.")
        return content

    def fetch_catalog(self) -> SourceCatalog:
        # A catalog is intentionally bounded; imported artworks are separately
        # capped by both their manifest byte count and image validation.
        raw = self.fetch(self.catalog_url, max_bytes=2 * 1024 * 1024)
        try:
            return SourceCatalog.model_validate_json(raw)
        except ValidationError as exc:
            raise CatalogError("invalid_catalog", "Catalog manifest does not match a supported verification-source-catalog version.") from exc

    def fetch_object(self, obj: SourceObject) -> bytes:
        content = self.fetch(self.object_url(obj.key), max_bytes=obj.bytes)
        if len(content) != obj.bytes:
            raise CatalogError("object_size_mismatch", f"Catalog object {obj.key} did not match its declared byte count.")
        if hashlib.sha256(content).hexdigest() != obj.sha256:
            raise CatalogError("object_hash_mismatch", f"Catalog object {obj.key} did not match its declared SHA-256.")
        return content


def configured_catalog() -> tuple[CatalogSource, SourceCatalog]:
    source = CatalogSource.configured()
    return source, source.fetch_catalog()


def selected_case(catalog: SourceCatalog, source_case_id: str) -> SourceCatalogCase | None:
    return next((case for case in catalog.cases if case.source_case_id == source_case_id), None)
