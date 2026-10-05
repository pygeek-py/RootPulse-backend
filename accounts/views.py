import hmac
import secrets

from django.conf import settings
from django.http import HttpResponseRedirect
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from . import github, onboarding, passwordless
from .models import EmailChallenge, User
from .serializers import (
    AuthResponseSerializer,
    EmailStartSerializer,
    OnboardingSerializer,
    OnboardingUpdateSerializer,
    RefreshResponseSerializer,
    UserSerializer,
    UserUpdateSerializer,
    VerifySerializer,
)
from .services import (
    audit,
    clear_refresh_cookie,
    issue_tokens,
    require_trusted_origin,
    set_refresh_cookie,
)

GITHUB_STATE_COOKIE = "gh_oauth_state"
GITHUB_STATE_PATH = "/api/v1/auth/github/"


class PublicAuthView(APIView):
    """Base for the unauthenticated auth endpoints: no JWT required, and a
    per-endpoint rate limit (`throttle_scope`, rates in settings)."""

    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]

    def get_authenticate_header(self, request):
        # With no authentication classes DRF has no challenge to send and
        # silently turns 401 into 403. A bad login must be a real 401.
        return 'Bearer realm="api"'


def _auth_response(user: User, status_code: int) -> Response:
    refresh, access = issue_tokens(user)
    response = Response(
        {"user": UserSerializer(user).data, "access_token": access}, status=status_code
    )
    set_refresh_cookie(response, refresh)
    return response


class EmailStartView(PublicAuthView):
    """Email a sign-in link + code. Always 204, whether or not the address has
    an account, so the endpoint can't be used to find out who is registered."""

    throttle_scope = "auth_email_start"
    purpose: str

    def post(self, request):
        serializer = EmailStartSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        sent = passwordless.start(email, self.purpose)
        audit("email_challenge_sent" if sent else "email_challenge_skipped", request, email=email)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["auth"], request=EmailStartSerializer, responses={204: None})
class RegisterView(EmailStartView):
    purpose = EmailChallenge.SIGNUP


@extend_schema(tags=["auth"], request=EmailStartSerializer, responses={204: None})
class LoginView(EmailStartView):
    purpose = EmailChallenge.LOGIN


@extend_schema(
    tags=["auth"],
    request=VerifySerializer,
    responses={
        200: AuthResponseSerializer,
        400: OpenApiResponse(description="Invalid or expired link/code"),
    },
)
class VerifyView(PublicAuthView):
    """Redeem the emailed link token, or email + code. Creates the account on
    first use. POST-only on purpose: mail scanners that prefetch the link with
    a GET must not burn the single-use token."""

    throttle_scope = "auth_verify"

    def post(self, request):
        serializer = VerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if data.get("token"):
                user, created = passwordless.verify_link(data["token"])
            else:
                user, created = passwordless.verify_code(data["email"], data["code"])
        except passwordless.InvalidChallenge:
            audit("email_verify_failed", request)
            return Response(
                {"detail": "This link or code is invalid or has expired.", "field_errors": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        audit("register" if created else "login", request, user, provider="email")
        return _auth_response(user, status.HTTP_200_OK)


@extend_schema(
    tags=["auth"],
    request=None,
    responses={200: RefreshResponseSerializer, 401: OpenApiResponse(description="No/expired")},
)
class RefreshView(PublicAuthView):
    """Trade the refresh cookie for a new access token, rotating the cookie.

    The old refresh token is blacklisted on use, so a stolen copy is only
    good until the real client next refreshes — and replaying it fails.
    """

    throttle_scope = "auth_refresh"

    def post(self, request):
        require_trusted_origin(request)

        raw = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)
        user = None
        if raw:
            try:
                old = RefreshToken(raw)
                user = User.objects.filter(pk=old["user_id"], is_active=True).first()
                if user is not None:
                    old.blacklist()
            except TokenError:
                user = None

        if user is None:
            response = Response({"detail": "Session expired."}, status=status.HTTP_401_UNAUTHORIZED)
            clear_refresh_cookie(response)
            return response

        refresh, access = issue_tokens(user)
        response = Response({"access_token": access})
        set_refresh_cookie(response, refresh)
        return response


@extend_schema(tags=["auth"], request=None, responses={204: None})
class LogoutView(PublicAuthView):
    throttle_scope = "auth_refresh"

    def post(self, request):
        require_trusted_origin(request)

        raw = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)
        if raw:
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                pass  # already expired/revoked — the goal state is the same
        response = Response(status=status.HTTP_204_NO_CONTENT)
        clear_refresh_cookie(response)
        return response


@extend_schema(tags=["auth"], request=UserUpdateSerializer, responses={200: UserSerializer})
class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(responses={200: UserSerializer})
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    def patch(self, request):
        serializer = UserUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(UserSerializer(request.user).data)


class OnboardingView(APIView):
    """The setup checklist: what is done, read from the person's own data."""

    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["auth"], responses={200: OnboardingSerializer})
    def get(self, request):
        return Response(onboarding.progress(request.user))

    @extend_schema(
        tags=["auth"], request=OnboardingUpdateSerializer, responses={200: OnboardingSerializer}
    )
    def patch(self, request):
        serializer = OnboardingUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        onboarding.set_dismissed(request.user, serializer.validated_data["dismissed"])
        return Response(onboarding.progress(request.user))


def _login_redirect(error: str) -> HttpResponseRedirect:
    response = HttpResponseRedirect(f"{settings.FRONTEND_URL}/login?error={error}")
    response.delete_cookie(GITHUB_STATE_COOKIE, path=GITHUB_STATE_PATH)
    return response


@extend_schema(tags=["auth"], request=None, responses={302: None})
class GitHubRedirectView(PublicAuthView):
    throttle_scope = "auth_github"

    def get(self, request):
        if not github.is_configured():
            return _login_redirect("github_not_configured")

        # `state` ties the callback to *this* browser, defeating login-CSRF.
        state = secrets.token_urlsafe(32)
        response = HttpResponseRedirect(github.authorize_url(state))
        response.set_cookie(
            GITHUB_STATE_COOKIE,
            state,
            max_age=600,
            path=GITHUB_STATE_PATH,
            secure=settings.AUTH_REFRESH_COOKIE_SECURE,
            httponly=True,
            samesite="Lax",  # GitHub returns via a cross-site top-level GET
        )
        return response


@extend_schema(tags=["auth"], request=None, responses={302: None})
class GitHubCallbackView(PublicAuthView):
    throttle_scope = "auth_github"

    def get(self, request):
        expected = request.COOKIES.get(GITHUB_STATE_COOKIE, "")
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        if not expected or not hmac.compare_digest(expected, state):
            return _login_redirect("github_state")
        if not code or request.query_params.get("error"):
            return _login_redirect("github_denied")

        try:
            profile = github.fetch_profile(code)
        except github.GitHubError:
            return _login_redirect("github_failed")

        user = User.objects.filter(github_id=profile.github_id).first()
        if user is None:
            user = User.objects.filter(email__iexact=profile.email).first()
            if user is not None:
                # GitHub only returns *verified* emails, so linking by email
                # can't be used to take over someone else's account.
                user.github_id = profile.github_id
                user.save(update_fields=["github_id"])
                audit("github_linked", request, user)
            else:
                user = User(username=secrets.token_hex(16), email=profile.email)
                user.github_id = profile.github_id
                user.set_unusable_password()
                user.save()
                audit("register", request, user, provider="github")

        if not user.is_active:
            return _login_redirect("account_disabled")

        audit("login", request, user, provider="github")
        refresh, _ = issue_tokens(user)
        response = HttpResponseRedirect(f"{settings.FRONTEND_URL}/auth/callback")
        set_refresh_cookie(response, refresh)
        response.delete_cookie(GITHUB_STATE_COOKIE, path=GITHUB_STATE_PATH)
        return response
