from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .access import has_perm


def require_perm(perm):
    """Login required, a school selected, and the user's role in it must grant `perm`."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.school is None or not has_perm(request.user, request.school, perm):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator
