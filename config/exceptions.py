from rest_framework.exceptions import ValidationError
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    """Uniform error body: `{"detail": "...", "field_errors": {...}}`.

    DRF's default shape varies (a dict of field errors for validation, a bare
    `detail` for everything else). The frontend client (lib/api/client.ts)
    reads `detail` and `field_errors`, so every error is normalised to those
    two keys and the client never has to guess.
    """
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, ValidationError):
        data = response.data
        if isinstance(data, dict):
            field_errors = {
                key: [str(e) for e in (value if isinstance(value, list) else [value])]
                for key, value in data.items()
            }
            non_field = field_errors.pop("non_field_errors", None)
            detail = non_field[0] if non_field else "Please correct the highlighted fields."
            response.data = {"detail": detail, "field_errors": field_errors}
        else:
            response.data = {"detail": str(data[0]) if data else "Invalid request."}
    elif isinstance(response.data, dict) and "detail" in response.data:
        response.data = {"detail": str(response.data["detail"])}
    return response
