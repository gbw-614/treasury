from app.field_library import get_field_definition

_GOVERNMENT_WARNING = get_field_definition("government_warning")

CANONICAL_WARNING_HEADING: str = _GOVERNMENT_WARNING.heading or ""
CANONICAL_WARNING_BODY: str = _GOVERNMENT_WARNING.body or ""
