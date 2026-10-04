from rest_framework.routers import SimpleRouter

from .views import MonitorViewSet

router = SimpleRouter()
router.register("", MonitorViewSet, basename="monitor")

urlpatterns = router.urls
