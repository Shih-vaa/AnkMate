from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .access import has_perm, permissions_for


def require_any_perm(*perms):
    """Login required, a school selected, and the user's roles must grant at least one of `perms`."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.school is None or not permissions_for(request.user, request.school).intersection(perms):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator


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
