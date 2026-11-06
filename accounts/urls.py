from django.urls import path

from .views import OwnPasswordChangeView

urlpatterns = [
    path("password/", OwnPasswordChangeView.as_view(), name="password_change"),
]
