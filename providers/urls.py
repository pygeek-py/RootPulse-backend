from rest_framework.routers import SimpleRouter

from .views import ProviderIncidentViewSet, ProviderViewSet

router = SimpleRouter()
router.register("providers", ProviderViewSet, basename="provider")
router.register("provider-incidents", ProviderIncidentViewSet, basename="provider-incident")

urlpatterns = router.urls
