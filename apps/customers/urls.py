from django.urls import path

from apps.customers import views

app_name = "customers"

urlpatterns = [
    path("", views.customer_list, name="customer_list"),
    path("new/", views.customer_form, name="customer_create"),
    path("statements/", views.statements, name="statements"),
    path("<int:pk>/", views.customer_detail, name="customer_detail"),
    path("<int:pk>/edit/", views.customer_form, name="customer_edit"),
    path("<int:pk>/pay/", views.customer_payment, name="customer_payment"),
    path("<int:pk>/statement/", views.customer_statement, name="customer_statement"),
    path("<int:pk>/restore/", views.customer_restore, name="customer_restore"),
    path("payments/<int:pk>/undo/", views.customer_payment_reverse, name="customer_payment_reverse"),
    path("<int:pk>/delete/", views.customer_delete, name="customer_delete"),
    path("<int:pk>/redeem/", views.customer_redeem, name="customer_redeem"),
]
