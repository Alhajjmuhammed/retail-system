from django.urls import path

from apps.pos import phone, views

app_name = "pos"

urlpatterns = [
    path("", views.till, name="till"),
    path("sw.js", views.service_worker, name="service_worker"),
    path("phone/", phone.phone, name="phone"),
    path("phone/lookup/", phone.lookup, name="phone_lookup"),
    path("phone/customers/", phone.customers, name="phone_customers"),
    path("phone/checkout/", phone.checkout, name="phone_checkout"),
    path("shift/open/", views.shift_open, name="shift_open"),
    path("shift/close/", views.shift_close, name="shift_close"),
    path("shift/<int:pk>/report/", views.shift_report, name="shift_report"),
    path("shift/<int:pk>/force-close/", views.shift_force_close, name="shift_force_close"),
    path("shift/cash/", views.cash_movement, name="cash_movement"),
    path("sales/", views.sale_list, name="sale_list"),
    path("fiscal/", views.fiscal_receipts, name="fiscal_receipts"),
    path("sales/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sales/<int:pk>/void/", views.sale_void, name="sale_void"),
    path("sales/<int:pk>/return/", views.sale_return, name="sale_return"),
    path("sales/<int:pk>/receipt/", views.receipt, name="receipt"),
]
