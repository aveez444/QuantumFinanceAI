# core/admin.py
from django.contrib import admin
from django.apps import apps
from . import models

# Optional: restrict non-superuser admins to their tenant's objects.
# Uncomment and adjust if you want tenant-scoped admin list views.
#
# class TenantAdminMixin:
#     def get_queryset(self, request):
#         qs = super().get_queryset(request)
#         if request.user.is_superuser:
#             return qs
#         # map request.user -> TenantUser (adjust attribute names if you renamed it)
#         tuser = models.TenantUser.objects.filter(user=request.user, is_active=True).first()
#         if tuser:
#             return qs.filter(tenant=tuser.tenant)
#         return qs.none()
#
#     def save_model(self, request, obj, form, change):
#         # auto-set tenant for new objects if not set
#         if not change and not getattr(obj, 'tenant', None):
#             tuser = models.TenantUser.objects.filter(user=request.user, is_active=True).first()
#             if tuser:
#                 obj.tenant = tuser.tenant
#         return super().save_model(request, obj, form, change)


# ===== Custom Admins for key models =====
class GLJournalLineInline(admin.TabularInline):
    model = models.GLJournalLine
    extra = 0
    readonly_fields = ('line_number',)


@admin.register(models.GLJournal)
class GLJournalAdmin(admin.ModelAdmin):
    inlines = [GLJournalLineInline]
    list_display = ('journal_number', 'tenant', 'posting_date', 'status', 'total_debit', 'total_credit')
    list_filter = ('status', 'posting_date', 'tenant')
    search_fields = ('journal_number', 'reference')


@admin.register(models.WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ('wo_number', 'tenant', 'product', 'status', 'due_date', 'priority', 'completion_percent')
    list_filter = ('status', 'priority', 'due_date', 'tenant')
    search_fields = ('wo_number', 'product__sku', 'product__product_name')
    readonly_fields = ('quantity_completed', 'quantity_scrapped')

    def completion_percent(self, obj):
        try:
            return f"{obj.completion_percentage:.1f}%"
        except Exception:
            return "0%"
    completion_percent.short_description = 'Completion'


@admin.register(models.Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'product_name', 'tenant', 'product_type', 'uom', 'standard_cost')
    list_filter = ('product_type', 'uom', 'tenant')
    search_fields = ('sku', 'product_name', 'category')


@admin.register(models.Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('company_name', 'subdomain', 'plan_type', 'is_active', 'created_at')
    search_fields = ('company_name', 'subdomain')


@admin.register(models.TenantUser)
class TenantUserAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'user', 'role', 'is_active', 'created_at')
    list_filter = ('role', 'is_active', 'tenant')
    search_fields = ('user__username', 'tenant__company_name')


@admin.register(models.Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ('employee_code', 'full_name', 'tenant', 'department', 'designation', 'hire_date')
    search_fields = ('employee_code', 'full_name', 'department')


@admin.register(models.ProductionEntry)
class ProductionEntryAdmin(admin.ModelAdmin):
    list_display = ('work_order', 'entry_datetime', 'equipment', 'operator', 'quantity_produced', 'quantity_rejected')
    list_filter = ('shift', 'entry_datetime', 'tenant')
    search_fields = ('work_order__wo_number',)


@admin.register(models.Warehouse)
class WarehouseAdmin(admin.ModelAdmin):
    list_display = ('warehouse_code', 'warehouse_name', 'tenant', 'location', 'manager')
    search_fields = ('warehouse_code', 'warehouse_name')


@admin.register(models.StockMovement)
class StockMovementAdmin(admin.ModelAdmin):
    list_display = ('movement_number', 'movement_type', 'product', 'warehouse', 'quantity', 'movement_date', 'tenant')
    list_filter = ('movement_type', 'movement_date', 'tenant')
    search_fields = ('movement_number', 'reference_doc')


@admin.register(models.CostCenter)
class CostCenterAdmin(admin.ModelAdmin):
    list_display = ('cost_center_code', 'name', 'tenant', 'manager')
    search_fields = ('cost_center_code', 'name')


@admin.register(models.AIQueryLog)
class AIQueryLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'tenant', 'user_query', 'was_successful', 'execution_time_ms')
    list_filter = ('was_successful', 'tenant')
    search_fields = ('user_query',)


@admin.register(models.AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = ('rule_name', 'trigger_type', 'is_enabled', 'last_executed', 'tenant')
    list_filter = ('trigger_type', 'is_enabled', 'tenant')
    search_fields = ('rule_name',)


