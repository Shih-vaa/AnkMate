from django import forms
from django.contrib.auth.forms import PasswordChangeForm


class OwnPasswordForm(PasswordChangeForm):
    """Change your own password. The new one must differ from the old (important after a temporary password)."""

    def clean_new_password1(self):
        new = self.cleaned_data.get("new_password1")
        old = self.cleaned_data.get("old_password")
        if new and old and new == old:
            raise forms.ValidationError("Please choose a password different from the current one.", code="same_password")
        return new
