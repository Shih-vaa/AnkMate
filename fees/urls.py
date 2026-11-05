from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="fee_home"),
    path("structure/", views.fee_structure, name="fee_structure"),
    path("student/<int:pk>/", views.student_account, name="fee_student"),
    path("invoice/<int:pk>/", views.invoice_detail, name="fee_invoice"),
    path("invoice/<int:pk>/cancel/", views.cancel_invoice_view, name="fee_invoice_cancel"),
    path("receipt/<int:pk>/", views.receipt, name="fee_receipt"),
    path("receipt/<int:pk>/reverse/", views.reverse_payment_view, name="fee_receipt_reverse"),
    path("collect/", views.collect, name="fee_collect"),
    path("collect/<int:student_pk>/", views.collect, name="fee_collect_student"),
    path("generate/", views.generate, name="fee_generate"),
    path("dues/", views.dues, name="fee_dues"),
]
