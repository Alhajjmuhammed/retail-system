from django.urls import path

from apps.org import views

app_name = "org"

urlpatterns = [
    path("business/", views.business, name="business"),
    path("branches/", views.branches, name="branches"),
    path("branches/new/", views.branch_form, name="branch_create"),
    path("branches/<int:pk>/", views.branch_form, name="branch_edit"),
    path("branches/<int:pk>/delete/", views.branch_delete, name="branch_delete"),
    path("registers/new/", views.register_create, name="register_create"),
    path("registers/<int:pk>/", views.register_edit, name="register_edit"),
    path("registers/<int:pk>/delete/", views.register_delete, name="register_delete"),
    path("devices/", views.devices, name="devices"),
    path("devices/<int:pk>/", views.device_update, name="device_update"),
]
