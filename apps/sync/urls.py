from django.urls import path

from apps.sync import views

app_name = "sync"

urlpatterns = [
    path("catalog/", views.catalog_snapshot, name="catalog"),
    path("customers/", views.customers_snapshot, name="customers"),
    path("sales/", views.push_sales, name="push_sales"),
    path("status/", views.sync_status, name="status"),
    path("carts/", views.cart_push, name="cart_push"),
    path("carts/<str:code>/", views.cart_pull, name="cart_pull"),
    path("devices/", views.device_register, name="device_register"),
]
