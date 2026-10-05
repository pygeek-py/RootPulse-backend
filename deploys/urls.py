from django.urls import path, register_converter
from rest_framework.routers import SimpleRouter

from .models import DeploySource
from .views import DeploySourceViewSet, DeployViewSet, DeployWebhookView


class SourceConverter:
    regex = "|".join(DeploySource.Type.values)

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: str) -> str:
        return value


register_converter(SourceConverter, "deploysource")

router = SimpleRouter()
router.register("deploy-sources", DeploySourceViewSet, basename="deploy-source")
router.register("deploys", DeployViewSet, basename="deploy")

urlpatterns = [
    path(
        "deploys/webhook/<deploysource:source>/<str:token>/",
        DeployWebhookView.as_view(),
        name="deploy-webhook",
    ),
    *router.urls,
]
