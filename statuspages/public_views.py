"""The unauthenticated surface: anyone with the link can read a published page.

Nothing here knows who is asking, so each rule is explicit: a page that doesn't exist and one
that isn't published are indistinguishable (the same 404); a password-protected page tells a
visitor its name and nothing else until a correct password has been given; and every
state-changing call is a POST that needs an unguessable token or a password. Each endpoint has
its own per-IP throttle.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import services
from .models import StatusPage
from .serializers import (
    MessageSerializer,
    PageSubscribeSerializer,
    PublicStatusPageSerializer,
    SubscriptionInfoSerializer,
    UnlockResultSerializer,
    UnlockSerializer,
)

NOT_FOUND = "This status page doesn't exist."


class PublicView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public_status"

    def page(self, slug: str) -> StatusPage:
        page = StatusPage.objects.filter(slug=slug, is_public=True).first()
        if page is None:
            raise NotFound(NOT_FOUND)
        return page

    def token(self, request) -> str | None:
        return request.META.get(services.UNLOCK_HEADER)

    @staticmethod
    def uncached(response: Response) -> Response:
        # A note switched off, or a page unpublished, has to disappear at once: never let a
        # browser or a CDN keep an old copy.
        response["Cache-Control"] = "no-store"
        return response


class PublicStatusPageView(PublicView):
    @extend_schema(
        tags=["public"],
        operation_id="public_status_pages_retrieve",
        parameters=[],
        responses={200: PublicStatusPageSerializer},
    )
    def get(self, request, slug):
        page = self.page(slug)
        if not services.may_view(page, self.token(request)):
            return self.uncached(Response(services.locked_payload(page)))
        return self.uncached(Response(services.build_public(page)))


class UnlockView(PublicView):
    throttle_scope = "public_unlock"

    @extend_schema(
        tags=["public"],
        request=UnlockSerializer,
        responses={200: UnlockResultSerializer},
    )
    def post(self, request, slug):
        page = self.page(slug)
        serializer = UnlockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if page.has_password and not services.password_matches(
            page, serializer.validated_data["password"]
        ):
            raise PermissionDenied("That password isn't right.")
        return self.uncached(
            Response(
                {
                    "token": services.make_unlock_token(page),
                    "expires_in": services.UNLOCK_MAX_AGE,
                }
            )
        )


class SubscribeView(PublicView):
    throttle_scope = "public_subscribe"

    @extend_schema(
        tags=["public"],
        request=PageSubscribeSerializer,
        responses={202: MessageSerializer},
    )
    def post(self, request, slug):
        page = self.page(slug)
        if not services.may_view(page, self.token(request)):
            raise PermissionDenied("Enter the password first.")
        serializer = PageSubscribeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.subscribe(page, serializer.validated_data["email"])
        # The same answer whether or not the address was already known.
        return Response(
            {"detail": "Check your inbox: we've sent a link to confirm your email."},
            status=status.HTTP_202_ACCEPTED,
        )


class SubscriptionTokenView(PublicView):
    throttle_scope = "public_token"

    def subscriber(self, slug: str, token: str):
        subscriber = services.subscriber_for_unsubscribe(token)
        if subscriber is None or subscriber.page.slug != slug:
            raise NotFound("That link isn't valid any more.")
        return subscriber


class ConfirmView(SubscriptionTokenView):
    @extend_schema(tags=["public"], request=None, responses={200: SubscriptionInfoSerializer})
    def post(self, request, slug, token):
        subscriber = services.confirm(token)
        if subscriber is None or subscriber.page.slug != slug:
            raise NotFound("That link isn't valid, or it has already been used.")
        return Response(_info(subscriber))


class UnsubscribeView(SubscriptionTokenView):
    @extend_schema(tags=["public"], responses={200: SubscriptionInfoSerializer})
    def get(self, request, slug, token):
        """Who the link is for, so the page can ask "unsubscribe a***@example.com?". Changes
        nothing: mail scanners open links, and must not be able to unsubscribe anyone."""
        return Response(_info(self.subscriber(slug, token)))

    @extend_schema(tags=["public"], request=None, responses={200: MessageSerializer})
    def post(self, request, slug, token):
        self.subscriber(slug, token).delete()
        return Response({"detail": "You're unsubscribed. We won't email you again."})


def _info(subscriber) -> dict:
    return {
        "page_name": subscriber.page.name,
        "page_slug": subscriber.page.slug,
        "email": services.masked(subscriber.email),
    }
