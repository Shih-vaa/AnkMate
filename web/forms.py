from django import forms
from django.contrib.auth.forms import AuthenticationForm

from accounts.models import Membership


class IdentifierLoginForm(AuthenticationForm):
    """One login screen for everyone. Which role you get comes from your account, not from a dropdown."""

    username = forms.CharField(
        label="Phone number, email or username",
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username", "autocapitalize": "none"}),
    )
    password = forms.CharField(
        label="Password or PIN",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        has_school = Membership.objects.filter(user=user, is_active=True, school__is_active=True).exists()
        if not user.is_superuser and not has_school:
            raise forms.ValidationError(
                "Your account is not linked to a school yet. Please contact the school office.",
                code="no_membership",
            )
