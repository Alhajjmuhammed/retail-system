from django.urls import path

from apps.inventory import views

app_name = "inventory"

urlpatterns = [
    path("", views.stock_list, name="stock_list"),
    path("movements/", views.movements, name="movements"),
    path("<int:pk>/adjust/", views.stock_adjust, name="stock_adjust"),
    path("transfers/", views.transfer_list, name="transfer_list"),
    path("transfers/new/", views.transfer_create, name="transfer_create"),
    path("transfers/<int:pk>/", views.transfer_detail, name="transfer_detail"),
    path("transfers/<int:pk>/cancel/", views.transfer_cancel, name="transfer_cancel"),
    path("transfer-lines/<int:pk>/delete/", views.transfer_line_delete, name="transfer_line_delete"),
    path("counts/", views.count_list, name="count_list"),
    path("counts/new/", views.count_create, name="count_create"),
    path("counts/<int:pk>/", views.count_detail, name="count_detail"),
    path("counts/<int:pk>/cancel/", views.count_cancel, name="count_cancel"),
    path("batches/", views.batch_list, name="batch_list"),
]
