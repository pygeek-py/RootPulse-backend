import hmac
import secrets

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.http import HttpResponseRedirect
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from . import github
from .models import User
from .serializers import (
    AuthResponseSerializer,
    LoginSerializer,
    PasswordForgotSerializer,
    PasswordResetSerializer,
    RefreshResponseSerializer,
    RegisterSerializer,
    UserSerializer,
    UserUpdateSerializer,
    validate_new_password,
)
from .services import (
    audit,
    clear_refresh_cookie,
    issue_tokens,
    require_trusted_origin,
    revoke_all_sessions,
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


@extend_schema(
    tags=["auth"],
    request=RegisterSerializer,
    responses={201: AuthResponseSerializer, 409: OpenApiResponse(description="Email taken")},
)
class RegisterView(PublicAuthView):
    throttle_scope = "auth_register"

    def post(self, request):
        email = str(request.data.get("email", "")).strip().lower()
        if email and User.objects.filter(email__iexact=email).exists():
            return Response(
                {
                    "detail": "An account with this email already exists.",
                    "field_errors": {"email": ["An account with this email already exists."]},
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        audit("register", request, user)
        return _auth_response(user, status.HTTP_201_CREATED)


@extend_schema(
    tags=["auth"],
    request=LoginSerializer,
    responses={200: AuthResponseSerializer, 401: OpenApiResponse(description="Bad credentials")},
)
class LoginView(PublicAuthView):
    throttle_scope = "auth_login"

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = authenticate(request, username=email, password=serializer.validated_data["password"])
        if user is None:
            # One generic message for wrong password, unknown email, inactive
            # account and GitHub-only accounts — nothing to enumerate.
            audit("login_failed", request, email=email)
            raise AuthenticationFailed("Invalid email or password.")

        audit("login", request, user)
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


@extend_schema(tags=["auth"], request=PasswordForgotSerializer, responses={204: None})
class PasswordForgotView(PublicAuthView):
    throttle_scope = "auth_password_forgot"

    def post(self, request):
        serializer = PasswordForgotSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            uid = urlsafe_base64_encode(str(user.pk).encode())
            token = default_token_generator.make_token(user)
            link = f"{settings.FRONTEND_URL}/reset-password/{uid}.{token}"
            send_mail(
                "Reset your RootPulse password",
                f"Use this link to choose a new password (valid for 1 hour):\n\n{link}\n\n"
                "If you didn't ask for this, you can ignore this email.",
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
            )
            audit("password_reset_requested", request, user)

        # Identical response whether or not the account exists.
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["auth"], request=PasswordResetSerializer, responses={204: None})
class PasswordResetView(PublicAuthView):
    throttle_scope = "auth_password_reset"

    def post(self, request):
        serializer = PasswordResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = self._user_for_token(serializer.validated_data["token"])
        if user is None:
            return Response(
                {"detail": "This reset link is invalid or has expired."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validate_new_password(serializer.validated_data["new_password"], user, "new_password")
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        revoke_all_sessions(user)  # whoever had a session before the reset is out
        audit("password_reset", request, user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _user_for_token(combined: str) -> User | None:
        uid, _, token = combined.partition(".")
        try:
            pk = urlsafe_base64_decode(uid).decode()
            user = User.objects.filter(pk=pk, is_active=True).first()
        except (ValueError, TypeError, UnicodeDecodeError, OverflowError, DjangoValidationError):
            # Includes a uid that decodes to something that isn't a UUID.
            return None
        if user is None or not default_token_generator.check_token(user, token):
            return None
        return user


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
