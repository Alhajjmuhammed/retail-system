from django.urls import path

from apps.purchasing import views

app_name = "purchasing"

urlpatterns = [
    path("suppliers/", views.supplier_list, name="supplier_list"),
    path("suppliers/new/", views.supplier_form, name="supplier_create"),
    path("suppliers/<int:pk>/", views.supplier_detail, name="supplier_detail"),
    path("suppliers/<int:pk>/edit/", views.supplier_form, name="supplier_edit"),
    path("suppliers/<int:pk>/pay/", views.supplier_pay, name="supplier_pay"),
    path("suppliers/<int:pk>/bill/", views.supplier_bill, name="supplier_bill"),
    path("suppliers/<int:pk>/delete/", views.supplier_delete, name="supplier_delete"),
    path("suppliers/<int:pk>/restore/", views.supplier_restore, name="supplier_restore"),
    path("supplier-payments/<int:pk>/reverse/", views.supplier_payment_reverse,
         name="supplier_payment_reverse"),
    path("orders/", views.order_list, name="order_list"),
    path("orders/new/", views.order_create, name="order_create"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path("orders/<int:pk>/cancel/", views.order_cancel, name="order_cancel"),
    path("orders/<int:pk>/print/", views.order_print, name="order_print"),
    path("order-lines/<int:pk>/delete/", views.order_line_delete, name="order_line_delete"),
    path("receive/", views.receipt_list, name="receipt_list"),
    path("receive/new/", views.receipt_create, name="receipt_create"),
    path("receive/<int:pk>/", views.receipt_detail, name="receipt_detail"),
    path("receive/<int:pk>/delete/", views.receipt_delete, name="receipt_delete"),
    path("receipt-lines/<int:pk>/delete/", views.receipt_line_delete, name="receipt_line_delete"),
]
