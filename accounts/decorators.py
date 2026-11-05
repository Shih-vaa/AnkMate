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


def require_perm(perm, all_scope=False):
    """Login required, a school selected, and the user's role in it must grant `perm`.
    `all_scope=True` additionally requires the permission to apply school-wide (not just to one's own classes
    or children): used for actions on the whole school's money, like recording payments."""

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.school is None or not has_perm(request.user, request.school, perm, all_scope=all_scope):
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return login_required(wrapped)

    return decorator
