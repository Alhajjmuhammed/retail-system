from django.urls import path

from apps.tenancy import views

app_name = "tenancy"

urlpatterns = [
    path("billing/", views.billing, name="billing"),
]
