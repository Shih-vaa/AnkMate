from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="attendance_home"),
    path("mark/<int:section_pk>/", views.mark, name="attendance_mark"),
    path("register/<int:section_pk>/", views.register, name="attendance_register"),
    path("student/<int:pk>/", views.student_attendance, name="attendance_student"),
]
