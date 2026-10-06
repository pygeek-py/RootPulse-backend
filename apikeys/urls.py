from rest_framework.routers import SimpleRouter

from .views import ApiKeyViewSet

router = SimpleRouter()
router.register("api-keys", ApiKeyViewSet, basename="api-key")

urlpatterns = router.urls
