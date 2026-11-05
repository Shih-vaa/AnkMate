from django.conf import settings
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = f"{settings.PRODUCT_NAME} administration"
admin.site.site_title = f"{settings.PRODUCT_NAME} admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("web.urls")),
]
