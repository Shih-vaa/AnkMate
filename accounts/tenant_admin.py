"""Django-admin base class that enforces school isolation and our RBAC.

Office staff use the Django admin as their data-entry screen for now. It is locked down so that:
  * non-superusers only ever see rows of their own school,
  * the `school` of a new row is filled in automatically and cannot be changed,
  * foreign-key dropdowns only offer that school's rows,
  * access is decided by the user's role (all-school scope only), not by Django's own permission tables.
"""
from django import forms

from core.models import School, TenantModel

from .access import has_perm
from .models import User


class TenantAdminMixin:
    manage_perm = "school.manage"
    view_perm = None  # falls back to manage_perm

    # -- permissions ---------------------------------------------------------
    def _allowed(self, request, perm):
        user = request.user
        if user.is_superuser:
            return True
        school = getattr(request, "school", None)
        return bool(school) and user.is_staff and has_perm(user, school, perm, all_scope=True)

    def _in_school(self, request, obj):
        if obj is None or request.user.is_superuser:
            return True
        return getattr(obj, "school_id", None) == request.school.id

    def has_module_permission(self, request):
        return self._allowed(request, self.view_perm or self.manage_perm)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request, self.view_perm or self.manage_perm) and self._in_school(request, obj)

    def has_add_permission(self, request):
        return self._allowed(request, self.manage_perm)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request, self.manage_perm) and self._in_school(request, obj)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request, self.manage_perm) and self._in_school(request, obj)

    # -- data isolation ------------------------------------------------------
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        school = getattr(request, "school", None)
        return qs.filter(school=school) if school else qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        school = getattr(request, "school", None)
        related = db_field.remote_field.model
        if not request.user.is_superuser:
            if issubclass(related, TenantModel):
                kwargs["queryset"] = related._default_manager.filter(school=school) if school else related.objects.none()
            elif related is User:
                kwargs["queryset"] = (
                    User.objects.filter(memberships__school=school).distinct() if school else User.objects.none()
                )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        school = getattr(request, "school", None)
        if school and not request.user.is_superuser:
            initial["school"] = school.pk
        return initial

    def get_form(self, request, obj=None, **kwargs):
        form_class = super().get_form(request, obj, **kwargs)
        school = getattr(request, "school", None)
        if request.user.is_superuser or school is None:
            return form_class

        class SchoolBoundForm(form_class):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                field = self.fields.get("school")
                if field is not None:
                    field.queryset = School.objects.filter(pk=school.pk)
                    field.initial = school.pk
                    field.disabled = True
                    field.widget = forms.HiddenInput()

        return SchoolBoundForm
