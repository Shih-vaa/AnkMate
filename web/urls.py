from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import TemplateView

from . import views
from .forms import IdentifierLoginForm

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path(
        "login/",
        auth_views.LoginView.as_view(
            authentication_form=IdentifierLoginForm, redirect_authenticated_user=True
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("students/", views.student_list, name="student_list"),
    path("students/<int:pk>/", views.student_detail, name="student_detail"),
    # PWA
    path(
        "manifest.webmanifest",
        TemplateView.as_view(template_name="web/manifest.webmanifest", content_type="application/manifest+json"),
        name="manifest",
    ),
    path("sw.js", views.service_worker, name="service_worker"),
    path("offline/", TemplateView.as_view(template_name="web/offline.html"), name="offline"),
]
