from django.contrib import messages
from django.contrib.auth.views import PasswordChangeView
from django.urls import reverse_lazy

from .forms import OwnPasswordForm


class OwnPasswordChangeView(PasswordChangeView):
    """Choose your own password: used voluntarily, and forced for anyone still on a temporary password."""

    template_name = "registration/password_change.html"
    form_class = OwnPasswordForm
    success_url = reverse_lazy("dashboard")

    def form_valid(self, form):
        form.user.must_change_password = False  # saved together with the new password
        response = super().form_valid(form)
        messages.success(self.request, "Your password has been changed.")
        return response
