from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="marks_home"),
    path("exams/", views.exams, name="marks_exams"),
    path("exams/<int:pk>/<str:action>/", views.exam_action, name="marks_exam_action"),
    path("enter/<int:exam_pk>/", views.entry_index, name="marks_entry_index"),
    path("enter/<int:exam_pk>/<int:section_pk>/<int:subject_pk>/", views.enter, name="marks_enter"),
    path("sheet/<int:exam_pk>/", views.sheet_index, name="marks_sheet_index"),
    path("sheet/<int:exam_pk>/<int:section_pk>/", views.sheet, name="marks_sheet"),
    path("student/<int:pk>/", views.student_progress, name="marks_student"),
    path("report/<int:pk>/", views.report_card, name="marks_report"),
    path("report/<int:pk>/remark/", views.remark_save, name="marks_remark"),
]
