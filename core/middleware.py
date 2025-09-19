# core/middleware.py
from django.core.cache import cache
from django.http import HttpResponseForbidden, JsonResponse, HttpResponseServerError
from django.shortcuts import get_object_or_404
from .models import Tenant, TenantUser
import threading
from django.db import OperationalError
import logging

logger = logging.getLogger("core")

# Thread-local tenant context
_thread_local = threading.local()

class TenantMiddleware:
    """
    MVP Middleware to set tenant context:
    - Subdomain for web
    - X-Tenant-ID for API
    - User -> TenantUser fallback
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Resolve tenant with defensive DB error handling
        try:
            tenant = self.resolve_tenant(request)
        except OperationalError as exc:
            # Log full exception (goes to your console + file logger)
            logger.exception("Database OperationalError while resolving tenant: %s", exc)
            # Return a safe 503 Service Unavailable so frontend sees a clear response
            return HttpResponseServerError("Service temporarily unavailable (database). Please try again shortly.")

        # Attach tenant to request + thread-local
        request.tenant = tenant
        _thread_local.tenant = tenant

        # Check user access if logged in
        if tenant and request.user.is_authenticated:
            if not TenantUser.objects.filter(user=request.user, tenant=tenant, is_active=True).exists():
                return HttpResponseForbidden("Access denied to this tenant")

        # Continue request cycle
        response = self.get_response(request)

        # Cleanup thread-local
        if hasattr(_thread_local, "tenant"):
            try:
                delattr(_thread_local, "tenant")
            except Exception:
                # best-effort cleanup; don't raise
                pass

        return response

    def resolve_tenant(self, request):
        """Pick tenant based on subdomain, header, or user"""
        tenant = None

        # 1. Subdomain
        host = request.get_host().split(":")[0]
        parts = host.split(".")
        if len(parts) >= 2:
            subdomain = parts[0]
            if subdomain not in ("www", ""):
                try:
                    tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
                except Tenant.DoesNotExist:
                    tenant = None

        # 2. API header
        if not tenant:
            tenant_id = request.headers.get("X-Tenant-ID")
            if tenant_id:
                try:
                    tenant = Tenant.objects.get(id=tenant_id, is_active=True)
                except Tenant.DoesNotExist:
                    tenant = None

        # 3. Authenticated user fallback
        if not tenant and request.user.is_authenticated:
            tenant_user = TenantUser.objects.filter(user=request.user, is_active=True).select_related("tenant").first()
            tenant = tenant_user.tenant if tenant_user else None

        return tenant


def get_current_tenant():
    """Get tenant from anywhere in the app"""
    return getattr(_thread_local, "tenant", None)


# Custom Manager for automatic tenant filtering
from django.db import models

class TenantManager(models.Manager):
    """Manager that automatically filters by tenant"""
    
    def get_queryset(self):
        tenant = get_current_tenant()
        if tenant:
            return super().get_queryset().filter(tenant=tenant)
        return super().get_queryset().none()  # No results if no tenant

    def all_tenants(self):
        """Override to get all records across tenants (admin use)"""
        return super().get_queryset()


# Tenant-aware base manager
class TenantAwareManager(models.Manager):
    """Manager that automatically filters by current tenant"""
    
    def get_queryset(self):
        tenant = get_current_tenant()
        if tenant:
            return super().get_queryset().filter(tenant=tenant)
        return super().get_queryset().none()


# Signals for automatic tenant assignment
from django.db.models.signals import pre_save
from django.dispatch import receiver

@receiver(pre_save)
def set_tenant_on_save(sender, instance, **kwargs):
    """Automatically set tenant on model save"""
    # Only apply to models that inherit from BaseModel
    if hasattr(instance, 'tenant_id') and not instance.tenant_id:
        tenant = get_current_tenant()
        if tenant:
            instance.tenant = tenant
