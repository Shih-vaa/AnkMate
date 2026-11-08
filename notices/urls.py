from django.urls import path

from . import views

urlpatterns = [
    path("", views.notice_list, name="notice_list"),
    path("new/", views.notice_new, name="notice_new"),
    path("<int:pk>/", views.notice_detail, name="notice_detail"),
    path("<int:pk>/edit/", views.notice_edit, name="notice_edit"),
    path("<int:pk>/withdraw/", views.notice_withdraw, name="notice_withdraw"),
    path("reminders/<int:pk>/open/", views.reminder_open, name="reminder_open"),
    path("reminders/read-all/", views.reminders_read_all, name="reminders_read_all"),
    path("fee-reminders/", views.fee_reminders, name="fee_reminders"),
]
