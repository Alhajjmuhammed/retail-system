from django.urls import path

from apps.catalog import views

app_name = "catalog"

urlpatterns = [
    path("", views.product_list, name="product_list"),
    path("new/", views.product_create, name="product_create"),
    path("<int:pk>/", views.product_edit, name="product_edit"),
    path("<int:pk>/delete/", views.product_delete, name="product_delete"),
    path("import/", views.product_import, name="product_import"),
    path("export/", views.product_export, name="product_export"),
    path("search/", views.product_search, name="product_search"),
    path("variants/<int:pk>/barcodes/", views.barcode_add, name="barcode_add"),
    path("barcodes/<int:pk>/delete/", views.barcode_delete, name="barcode_delete"),
    path("categories/", views.taxonomy, {"only": "category"}, name="categories"),
    path("settings/taxonomy/", views.taxonomy, name="taxonomy"),
    path("settings/taxonomy/<str:kind>/<int:pk>/", views.taxonomy_edit, name="taxonomy_edit"),
    path("settings/taxonomy/<str:kind>/<int:pk>/delete/", views.taxonomy_delete, name="taxonomy_delete"),
    path("settings/prices/", views.price_lists, name="price_lists"),
    path("settings/tiles/", views.tiles, name="tiles"),
]
