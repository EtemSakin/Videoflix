"""URL patterns for account endpoints."""

from django.urls import path

from accounts.views import AccountActivationAPIView, RegistrationAPIView

urlpatterns = [
    path("register/", RegistrationAPIView.as_view(), name="register"),
    path(
        "activate/<str:uidb64>/<str:token>/",
        AccountActivationAPIView.as_view(),
        name="activate",
    ),
]
