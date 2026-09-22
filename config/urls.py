from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from apps.accounts import views as account_views
from apps.core.views import healthz

urlpatterns = [
    path("healthz", healthz),
    # Asked for by every browser before it reads the page's own icon link.
    path("favicon.ico", RedirectView.as_view(url="/static/img/easyfix-icon.png", permanent=True)),
    path("django-admin/", admin.site.urls),
    path("signup/", account_views.signup, name="signup"),
    path("", include("apps.accounts.urls")),
    path("pos/", include("apps.pos.urls")),
    path("products/", include("apps.catalog.urls")),
    path("stock/", include("apps.inventory.urls")),
    path("purchasing/", include("apps.purchasing.urls")),
    path("customers/", include("apps.customers.urls")),
    path("finance/", include("apps.finance.urls")),
    path("reports/", include("apps.reports.urls")),
    path("platform/", include("apps.tenancy.admin_urls")),
    path("api/v1/sync/", include("apps.sync.urls")),
    path("settings/messages/", include("apps.notifications.urls")),
    path("settings/", include("apps.org.urls")),
    path("settings/", include("apps.tenancy.urls")),
    path("", include("apps.core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
