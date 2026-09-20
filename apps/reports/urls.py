from django.urls import path

from apps.reports import views

app_name = "reports"

urlpatterns = [
    path("", views.index, name="index"),
    path("margin/", views.margin, name="margin"),
    path("stock/", views.stock_value, name="stock_value"),
    path("staff/", views.staff, name="staff"),
    path("export/", views.export, name="export"),
]
