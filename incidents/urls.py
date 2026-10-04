from rest_framework.routers import SimpleRouter

from .views import IncidentViewSet

router = SimpleRouter()
router.register("", IncidentViewSet, basename="incident")

urlpatterns = router.urls
