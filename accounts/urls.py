from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="auth-register"),
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("refresh/", views.RefreshView.as_view(), name="auth-refresh"),
    path("me/", views.MeView.as_view(), name="auth-me"),
    path("password/forgot/", views.PasswordForgotView.as_view(), name="auth-password-forgot"),
    path("password/reset/", views.PasswordResetView.as_view(), name="auth-password-reset"),
    path("github/redirect/", views.GitHubRedirectView.as_view(), name="auth-github-redirect"),
    path("github/callback/", views.GitHubCallbackView.as_view(), name="auth-github-callback"),
]
