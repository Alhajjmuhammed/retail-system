"""
One namespace for everything about people.

Paths are written in full rather than nested, so the settings screens sit
under /settings/ where an owner expects them while still resolving as
``accounts:roles``.
"""

from django.urls import path

from apps.accounts import audit_views, views

app_name = "accounts"

urlpatterns = [
    path("accounts/login/", views.LoginView.as_view(), name="login"),
    path("accounts/logout/", views.LogoutView.as_view(), name="logout"),
    path("accounts/password/", views.password_change, name="password_change"),
    path("accounts/password/reset/", views.PasswordResetView.as_view(), name="password_reset"),
    path("accounts/password/reset/sent/",
         views.auth_views.PasswordResetDoneView.as_view(
             template_name="accounts/password_reset_sent.html"),
         name="password_reset_done"),
    path("accounts/password/reset/<uidb64>/<token>/",
         views.PasswordResetConfirmView.as_view(), name="password_reset_confirm"),
    path("accounts/no-tenant/", views.no_tenant, name="no_tenant"),
    path("accounts/switch/", views.switch_tenant, name="switch"),
    path("accounts/branch/", views.switch_branch, name="switch_branch"),
    path("join/<uuid:token>/", views.accept_invitation, name="accept_invitation"),

    path("settings/roles/", views.roles, name="roles"),
    path("settings/roles/new/", views.role_create, name="role_create"),
    path("settings/roles/<int:pk>/", views.role_edit, name="role_edit"),
    path("settings/roles/<int:pk>/delete/", views.role_delete, name="role_delete"),
    path("settings/roles/<int:pk>/copy/", views.role_duplicate, name="role_duplicate"),
    path("settings/staff/", views.staff, name="staff"),
    path("settings/staff/new/", views.staff_create, name="staff_create"),
    path("settings/staff/invite/", views.staff_invite, name="staff_invite"),
    path("settings/staff/invite/<int:pk>/", views.staff_invite_sent, name="staff_invite_sent"),
    path("settings/staff/invite/<int:pk>/cancel/", views.staff_invite_cancel, name="staff_invite_cancel"),
    path("settings/staff/<int:pk>/", views.staff_edit, name="staff_edit"),
    path("settings/staff/<int:pk>/remove/", views.staff_remove, name="staff_remove"),
    path("settings/staff/<int:pk>/toggle/", views.staff_toggle, name="staff_toggle"),

    path("settings/audit/", audit_views.audit_log, name="audit_log"),
    path("settings/audit/<int:pk>/", audit_views.audit_entry, name="audit_entry"),
]
