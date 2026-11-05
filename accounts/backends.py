import re

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from .models import normalize_phone

_PHONE_LIKE = re.compile(r"[\d\s+\-]+")


class IdentifierBackend(ModelBackend):
    """Log in with username, email or phone number (parents use phone + PIN/password)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        User = get_user_model()
        if not username or password is None:
            return None
        ident = username.strip()
        query = Q(username__iexact=ident) | Q(email__iexact=ident)
        if _PHONE_LIKE.fullmatch(ident):
            phone = normalize_phone(ident)
            if len(phone) == 10:
                query |= Q(phone=phone)
        candidates = list(User.objects.filter(query).distinct()[:5])
        for user in candidates:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        if not candidates:
            User().set_password(password)  # keep timing similar for unknown identifiers
        return None
