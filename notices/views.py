from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from academics.models import Grade
from accounts.access import has_perm
from accounts.decorators import require_perm

from . import reminders, services
from .forms import EditNoticeForm, NewNoticeForm
from .models import Notice, NoticeRead
from .reminders import ReminderError
from .services import NoticeError

PAGE_SIZE = 20
SHOWN_REMINDERS = 20     # unopened reminders listed at the top of the page
SHOWN_EARLIER = 5        # opened ones kept in view below them
MAX_NAMES_IN_MESSAGE = 8


# -- reading -----------------------------------------------------------------------------------

@require_perm("notices.view")
def notice_list(request):
    school, user = request.school, request.user
    can_post = services.can_post(user, school)
    school_wide_poster = has_perm(user, school, "notices.post", all_scope=True)

    view = request.GET.get("view", "inbox")
    if view == "mine" and can_post:
        notices = services.visible_notices(user, school, include_dead=True).filter(author=user)
    elif view == "all" and school_wide_poster:
        notices = Notice.objects.filter(school=school)  # everything ever posted, withdrawn ones included
    else:
        view, notices = "inbox", services.visible_notices(user, school)

    category = request.GET.get("category", "")
    if category in Notice.Category.values:
        notices = notices.filter(category=category)
    else:
        category = ""

    page = Paginator(services.with_audience(notices), PAGE_SIZE).get_page(request.GET.get("page"))
    seen = set(NoticeRead.objects.filter(user=user, notice__in=[n.pk for n in page]).values_list("notice_id", flat=True))
    today = services.today()
    for notice in page:
        notice.unread = notice.pk not in seen and notice.author_id != user.id and notice.is_live(today)
        notice.dead = not notice.is_live(today)
    params = request.GET.copy()
    params.pop("page", None)

    ctx = {
        "page": page, "view": view, "category": category, "categories": Notice.Category.choices,
        "can_post": can_post, "school_wide_poster": school_wide_poster,
        "can_remind": reminders.can_send_fee_reminders(user, school),
        "query_string": params.urlencode(), "today": today,
    }
    if view == "inbox":
        unread = list(reminders.unread_reminders(user, school)[:SHOWN_REMINDERS])
        earlier = [] if len(unread) >= SHOWN_REMINDERS else list(
            reminders.reminders_for(user, school).filter(read_at__isnull=False)[:SHOWN_EARLIER]
        )
        ctx.update(reminder_list=unread + earlier, unread_reminder_count=len(unread))
    return render(request, "notices/list.html", ctx)


@require_perm("notices.view")
def notice_detail(request, pk):
    school, user = request.school, request.user
    notice = services.get_visible_notice(user, school, pk)
    if notice is None:
        raise Http404
    today = services.today()
    if notice.is_live(today):
        services.mark_read(notice, user)
    can_manage = services.can_manage_notice(user, school, notice)
    return render(
        request,
        "notices/detail.html",
        {
            "notice": notice, "audience": notice.audience_labels(), "can_manage": can_manage,
            "reach": services.reach(notice) if can_manage and not notice.is_withdrawn else None,
            "expired": not notice.is_withdrawn and not notice.is_live(today),
            "can_fix_logins": has_perm(user, school, "students.manage", all_scope=True),
        },
    )


@require_perm("notices.view")
def reminder_open(request, pk):
    """Opening a reminder marks it read and takes the person to the page it is about."""
    reminder = get_object_or_404(reminders.reminders_for(request.user, request.school), pk=pk)
    reminders.mark_reminder_read(reminder)
    return redirect(reminders.safe_link(reminder.link) or "notice_list")


@require_perm("notices.view")
@require_POST
def reminders_read_all(request):
    reminders.mark_all_read(request.user, request.school)
    return redirect("notice_list")


# -- writing -----------------------------------------------------------------------------------

@require_perm("notices.post")
def notice_new(request):
    school, user = request.school, request.user
    audience = services.postable_audience(user, school)
    if not audience.any:  # allowed to post, but there is nobody they may address (a teacher with no classes yet)
        return render(request, "notices/compose.html", {"nobody": True}, status=403)

    form = NewNoticeForm(request.POST or None, audience=audience)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            notice = services.create_notice(
                school, user, title=data["title"], body=data["body"], category=data["category"],
                expires_on=data["expires_on"], **form.audience_choices(),
            )
        except NoticeError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "Your notice has been posted.")
            return redirect("notice_detail", pk=notice.pk)
    return render(request, "notices/compose.html", {"form": form, "audience": audience})


@require_perm("notices.post")
def notice_edit(request, pk):
    school, user = request.school, request.user
    notice = get_object_or_404(services.visible_notices(user, school, include_dead=True), pk=pk)
    if not services.can_manage_notice(user, school, notice):
        raise Http404
    if notice.is_withdrawn:
        messages.error(request, "This notice has been withdrawn, so it can't be changed.")
        return redirect("notice_detail", pk=notice.pk)

    initial = {"title": notice.title, "body": notice.body, "category": notice.category, "expires_on": notice.expires_on}
    form = EditNoticeForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            services.update_notice(
                notice, user, title=data["title"], body=data["body"], category=data["category"], expires_on=data["expires_on"]
            )
        except NoticeError as error:
            form.add_error(None, str(error))
        else:
            messages.success(request, "The notice has been updated.")
            return redirect("notice_detail", pk=notice.pk)
    return render(request, "notices/compose.html", {"form": form, "notice": notice, "audience_labels": notice.audience_labels()})


@require_perm("notices.post")
@require_POST
def notice_withdraw(request, pk):
    school, user = request.school, request.user
    notice = get_object_or_404(services.visible_notices(user, school, include_dead=True), pk=pk)
    try:
        services.withdraw_notice(notice, user)
    except NoticeError:
        raise Http404
    messages.success(request, "The notice has been withdrawn. Readers no longer see it.")
    return redirect("notice_list")


# -- fee reminders -----------------------------------------------------------------------------

def _names(students):
    names = [s.full_name for s in students]
    shown = ", ".join(names[:MAX_NAMES_IN_MESSAGE])
    return shown + (f" and {len(names) - MAX_NAMES_IN_MESSAGE} more" if len(names) > MAX_NAMES_IN_MESSAGE else "")


@require_perm("fees.collect", all_scope=True)
def fee_reminders(request):
    school, user = request.school, request.user
    grade_id = request.GET.get("grade") or request.POST.get("grade") or ""
    grade_id = int(grade_id) if grade_id.isdigit() else None
    here = reverse("fee_reminders") + (f"?grade={grade_id}" if grade_id else "")

    if request.method == "POST":
        try:
            result = reminders.send_fee_reminders(school, request.POST.getlist("student"), user)
        except (ReminderError, ValueError) as error:
            messages.error(request, str(error) if isinstance(error, ReminderError) else "Please choose families from the list.")
            return redirect(here)
        if result.reminders:
            messages.success(
                request,
                f"Sent {result.reminders} reminder{'s' * (result.reminders != 1)} to {result.students} "
                f"{'families' if result.students != 1 else 'family'}. They see it under Notices.",
            )
        else:
            messages.error(request, "No reminders were sent.")
        if result.recent:
            messages.error(request, f"{result.recent} {'family was' if result.recent == 1 else 'families were'} reminded in the last week, so they were skipped.")
        if result.not_overdue:
            messages.error(request, f"{result.not_overdue} {'family has' if result.not_overdue == 1 else 'families have'} no overdue fees any more and was left out.")
        if result.no_login:
            messages.error(request, f"No parent login to send to for: {_names(result.no_login)}. Please phone these families.")
        return redirect(here)

    rows = reminders.fee_reminder_rows(school, grade_id=grade_id)
    counts = {state: sum(1 for r in rows if r["state"] == state) for state in ("ready", "recent", "no_login")}
    return render(
        request,
        "notices/fee_reminders.html",
        {"rows": rows, "counts": counts, "grades": Grade.objects.filter(school=school), "grade_id": grade_id,
         "cooldown_days": reminders.FEE_COOLDOWN_DAYS},
    )
