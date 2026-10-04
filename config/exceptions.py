from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler


def _flatten(prefix: str, value, out: dict[str, list[str]]) -> None:
    """Turn DRF's nested error structure into `{"config.timeout_seconds": [...]}`.

    Nested serializers give dicts, and list fields give `{index: [...]}` dicts;
    list-item indexes are folded into the list's own path so the frontend can
    attach an error to a form field without knowing about positions.
    """
    if isinstance(value, dict):
        for key, inner in value.items():
            child = (
                prefix
                if isinstance(key, int) or str(key).isdigit()
                else f"{prefix}.{key}" if prefix else str(key)
            )
            _flatten(child, inner, out)
    elif isinstance(value, list):
        if value and all(isinstance(item, (dict, list)) for item in value):
            for item in value:
                _flatten(prefix, item, out)
        else:
            out.setdefault(prefix, []).extend(str(e) for e in value if str(e))
    else:
        out.setdefault(prefix, []).append(str(value))


def api_exception_handler(exc, context):
    """Uniform error body: `{"detail": "...", "field_errors": {...}}`.

    DRF's default shape varies (a dict of field errors for validation, a bare
    `detail` for everything else). The frontend client (lib/api/client.ts)
    reads `detail` and `field_errors`, so every error is normalised to those
    two keys and the client never has to guess. Nested errors (a monitor's
    `config`) are flattened to dotted paths like `config.timeout_seconds`.
    """
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, ValidationError):
        data = response.data
        if isinstance(data, dict):
            field_errors: dict[str, list[str]] = {}
            _flatten("", data, field_errors)
            non_field = field_errors.pop("non_field_errors", None)
            detail = non_field[0] if non_field else "Please correct the highlighted fields."
            response.data = {"detail": detail, "field_errors": field_errors}
        else:
            response.data = {"detail": str(data[0]) if data else "Invalid request."}
    elif isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"detail": str(response.data["detail"])}
    return response
