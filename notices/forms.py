from django import forms

from accounts.models import Role
from academics.models import Grade, Section

from . import services
from .models import Notice


class _Text(forms.Form):
    title = forms.CharField(max_length=services.TITLE_MAX, strip=True, widget=forms.TextInput(attrs={"autocomplete": "off"}))
    body = forms.CharField(
        label="Notice", max_length=services.BODY_MAX, strip=True,
        widget=forms.Textarea(attrs={"rows": 9}), help_text="Plain text. Blank lines start a new paragraph; web addresses become links.",
    )
    category = forms.ChoiceField(choices=Notice.Category.choices, initial=Notice.Category.GENERAL)
    expires_on = forms.DateField(
        label="Show until (optional)", required=False, widget=forms.DateInput(attrs={"type": "date"}),
        help_text="After this day it disappears for readers. Leave empty to keep it up.",
    )


class EditNoticeForm(_Text):
    """Fixing the wording. Who it is for can't change after posting: post a new notice to reach other people."""


class NewNoticeForm(_Text):
    """The audience choices offered depend on who is posting: a teacher only sees the sections they teach."""

    everyone = forms.BooleanField(required=False, label="Everyone in the school")
    roles = forms.ModelMultipleChoiceField(queryset=Role.objects.none(), required=False, widget=forms.CheckboxSelectMultiple, label="By role")
    grades = forms.ModelMultipleChoiceField(queryset=Grade.objects.none(), required=False, widget=forms.CheckboxSelectMultiple, label="Whole classes")
    sections = forms.ModelMultipleChoiceField(queryset=Section.objects.none(), required=False, widget=forms.CheckboxSelectMultiple, label="Sections")
    field_order = ["title", "body", "category", "expires_on", "everyone", "roles", "grades", "sections"]

    def __init__(self, *args, audience, **kwargs):
        super().__init__(*args, **kwargs)
        if audience.everyone:
            self.fields["roles"].queryset = audience.roles
            self.fields["grades"].queryset = audience.grades
        else:  # a teacher can't address the whole school, a role or a whole class
            for name in ("everyone", "roles", "grades"):
                del self.fields[name]
        self.fields["sections"].queryset = audience.sections
        self.fields["sections"].label_from_instance = lambda section: f"{section.grade.name}-{section.name}"

    def audience_choices(self):
        data = self.cleaned_data
        return {
            "everyone": bool(data.get("everyone")),
            "role_ids": [r.pk for r in data.get("roles", [])],
            "grade_ids": [g.pk for g in data.get("grades", [])],
            "section_ids": [s.pk for s in data.get("sections", [])],
        }
