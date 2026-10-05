from django.urls import path
from rest_framework.routers import SimpleRouter

from . import public_views
from .views import StatusPageViewSet

router = SimpleRouter()
router.register("status-pages", StatusPageViewSet, basename="status-page")

SLUG = "<slug:slug>"
PUBLIC = f"public/status-pages/{SLUG}/"

urlpatterns = [
    path(PUBLIC, public_views.PublicStatusPageView.as_view(), name="public-status-page"),
    path(f"{PUBLIC}unlock/", public_views.UnlockView.as_view(), name="public-status-unlock"),
    path(
        f"{PUBLIC}subscribe/", public_views.SubscribeView.as_view(), name="public-status-subscribe"
    ),
    path(
        f"{PUBLIC}confirm/<str:token>/",
        public_views.ConfirmView.as_view(),
        name="public-status-confirm",
    ),
    path(
        f"{PUBLIC}unsubscribe/<str:token>/",
        public_views.UnsubscribeView.as_view(),
        name="public-status-unsubscribe",
    ),
    *router.urls,
]
