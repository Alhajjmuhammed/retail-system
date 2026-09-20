from django.urls import path

from apps.finance import views

app_name = "finance"

urlpatterns = [
    path("expenses/", views.expense_list, name="expense_list"),
    path("expenses/new/", views.expense_create, name="expense_create"),
    path("expenses/<int:pk>/", views.expense_edit, name="expense_edit"),
    path("expenses/<int:pk>/delete/", views.expense_delete, name="expense_delete"),
    path("expenses/<int:pk>/receipt/", views.expense_attachment, name="expense_attachment"),
    path("expense-categories/<int:pk>/delete/", views.expense_category_delete, name="expense_category_delete"),
    path("expenses/<int:pk>/approve/", views.expense_approve, name="expense_approve"),
    path("cashups/", views.cashups, name="cashups"),
    path("cashups/<int:pk>/approve/", views.cashup_approve, name="cashup_approve"),
]
