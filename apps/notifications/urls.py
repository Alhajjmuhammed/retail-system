from django.urls import path

from apps.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.message_log, name="message_log"),
    path("templates/<int:pk>/", views.template_edit, name="template_edit"),
]
