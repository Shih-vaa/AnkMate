from django.urls import path

from . import views

urlpatterns = [
    path("", views.hub, name="setup_home"),
    path("people/<str:word>/", views.people, name="setup_people"),
    path("people/<str:word>/bulk/", views.bulk, name="setup_bulk"),
    path("people/<str:word>/<int:pk>/login/", views.create_person_login, name="setup_create_login"),
    path("accounts/<int:user_pk>/<str:action>/", views.account_action, name="setup_account_action"),
    path("import/", views.import_start, name="setup_import"),
    path("import/template/<str:word>/", views.import_template, name="setup_import_template"),
    path("import/<int:pk>/", views.import_preview, name="setup_import_preview"),
    path("import/<int:pk>/problems.csv", views.import_problems, name="setup_import_problems"),
]
