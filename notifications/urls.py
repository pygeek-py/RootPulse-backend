from django.urls import path, register_converter
from rest_framework.routers import SimpleRouter

from .integration_views import (
    IntegrationsView,
    OAuthCallbackView,
    OAuthStartView,
    TelegramLinkStatusView,
    TelegramStartView,
    TelegramWebhookView,
)
from .views import AlertContactViewSet, DeliveryViewSet, VapidKeyView


class ProviderConverter:
    regex = "discord|slack"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


register_converter(ProviderConverter, "regex")

router = SimpleRouter()
router.register("alert-contacts", AlertContactViewSet, basename="alert-contact")
router.register("notifications", DeliveryViewSet, basename="notification")

urlpatterns = [
    path("notifications/vapid-key/", VapidKeyView.as_view(), name="vapid-key"),
    path("integrations/", IntegrationsView.as_view(), name="integrations"),
    path("integrations/telegram/start/", TelegramStartView.as_view(), name="telegram-start"),
    path(
        "integrations/telegram/links/<uuid:pk>/",
        TelegramLinkStatusView.as_view(),
        name="telegram-link",
    ),
    path("integrations/telegram/webhook/", TelegramWebhookView.as_view(), name="telegram-webhook"),
    path(
        "integrations/<regex:provider>/start/",
        OAuthStartView.as_view(),
        name="oauth-start",
    ),
    path(
        "integrations/<regex:provider>/callback/",
        OAuthCallbackView.as_view(),
        name="oauth-callback",
    ),
    *router.urls,
]
