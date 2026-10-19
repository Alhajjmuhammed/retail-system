from django.urls import path

from apps.reports import views

app_name = "reports"

urlpatterns = [
    path("", views.index, name="index"),
    path("margin/", views.margin, name="margin"),
    path("stock/", views.stock_value, name="stock_value"),
    path("staff/", views.staff, name="staff"),
    path("export/", views.export, name="export"),
    path("all/", views.hub, name="hub"),
    path("money-in/", views.money_in, name="money_in"),
    path("owed-to-you/", views.receivables, name="receivables"),
    path("owed-to-suppliers/", views.payables, name="payables"),
    path("invoices/", views.paperwork, name="paperwork"),
    path("profit-and-loss/", views.pnl, name="pnl"),
    path("balance-sheet/", views.balance, name="balance"),
    path("vat/", views.vat, name="vat"),
    path("best-sellers/", views.best, name="best"),
    path("purchases/", views.purchases, name="purchases"),
]
