from core.models import School

from .models import Membership

SESSION_KEY = "school_id"


def allowed_schools(user):
    member_of = Membership.objects.filter(user=user, is_active=True).values("school_id")
    return School.objects.filter(is_active=True, pk__in=member_of).order_by("name")


class SchoolMiddleware:
    """Sets request.school: the school the logged-in user is currently working in.

    A user with roles in several schools can switch later; the choice is validated against
    their memberships on every request, so the session can't be used to reach another school.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.school = None
        user = request.user
        if user.is_authenticated:
            schools = list(allowed_schools(user))
            chosen = next((s for s in schools if s.pk == request.session.get(SESSION_KEY)), None)
            if chosen is None and schools:
                chosen = schools[0]
                request.session[SESSION_KEY] = chosen.pk
            request.school = chosen
        return self.get_response(request)


class PasswordChangeRequiredMiddleware:
    """While someone is still on a temporary password, the only things they can reach are the change-password
    page, logging out, and the static files the page needs. Everything else sends them to choose their own password."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.must_change_password:
            from django.contrib.staticfiles.storage import staticfiles_storage
            from django.shortcuts import redirect
            from django.urls import reverse

            allowed = {reverse(name) for name in ("password_change", "logout", "service_worker", "manifest", "offline")}
            if request.path not in allowed and not request.path.startswith(staticfiles_storage.base_url):
                return redirect("password_change")
        return self.get_response(request)
