from django.urls import path

from apps.org import views

app_name = "org"

urlpatterns = [
    path("", views.settings_home, name="settings_home"),
    path("business/", views.business, name="business"),
    path("branches/", views.branches, name="branches"),
    path("branches/new/", views.branch_form, name="branch_create"),
    path("branches/<int:pk>/", views.branch_form, name="branch_edit"),
    path("branches/<int:pk>/delete/", views.branch_delete, name="branch_delete"),
    path("registers/new/", views.register_create, name="register_create"),
    path("registers/<int:pk>/", views.register_edit, name="register_edit"),
    path("registers/<int:pk>/delete/", views.register_delete, name="register_delete"),
    path("currencies/", views.currencies, name="currencies"),
    path("banks/", views.bank_accounts, name="bank_accounts"),
    path("banks/new/", views.bank_account_create, name="bank_account_create"),
    path("banks/<int:pk>/", views.bank_account_edit, name="bank_account_edit"),
    path("banks/<int:pk>/delete/", views.bank_account_delete, name="bank_account_delete"),
    path("devices/", views.devices, name="devices"),
    path("devices/<int:pk>/", views.device_update, name="device_update"),
]
