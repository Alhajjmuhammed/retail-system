from django.urls import path

from apps.selling import views

app_name = "selling"

urlpatterns = [
    path("quotations/", views.quotation_list, name="quotation_list"),
    path("quotations/new/", views.quotation_create, name="quotation_create"),
    path("quotations/<int:pk>/", views.quotation_detail, name="quotation_detail"),
    path("quotations/<int:pk>/print/", views.quotation_print, name="quotation_print"),
    path("quotations/<int:pk>/delete/", views.quotation_delete, name="quotation_delete"),
    path("invoices/", views.invoice_list, name="invoice_list"),
    path("invoices/new/", views.invoice_create, name="invoice_create"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice_detail"),
    path("invoices/<int:pk>/print/", views.invoice_print, name="invoice_print"),
    path("invoices/<int:pk>/deliver/", views.delivery_create, name="delivery_create"),
    path("deliveries/<int:pk>/print/", views.delivery_print, name="delivery_print"),
]
