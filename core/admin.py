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


class ProductImageInline(admin.TabularInline):
    model = models.ProductImage
    extra = 3  # Show 3 empty forms by default
    readonly_fields = ('image_preview',)
    
    def image_preview(self, obj):
        if obj.image:
            return f'<img src="{obj.image.url}" style="max-height: 100px; max-width: 100px;" />'
        return "No image"
    image_preview.allow_tags = True
    image_preview.short_description = 'Preview'

@admin.register(models.Product)
class ProductAdmin(admin.ModelAdmin):
    inlines = [ProductImageInline]  # Add this line
    list_display = ('sku', 'product_name', 'tenant', 'product_type', 'uom', 'standard_cost', 'has_image', 'additional_images_count')
    list_filter = ('product_type', 'uom', 'tenant')
    search_fields = ('sku', 'product_name', 'category')
    readonly_fields = ('image_preview',)

    def has_image(self, obj):
        return bool(obj.primary_image)
    has_image.boolean = True
    has_image.short_description = 'Has Primary Image'

    def additional_images_count(self, obj):
        return obj.additional_images.count()
    additional_images_count.short_description = 'Additional Images'

    def image_preview(self, obj):
        if obj.primary_image:
            return f'<img src="{obj.primary_image.url}" style="max-height: 200px; max-width: 200px;" />'
        return "No primary image"
    image_preview.allow_tags = True
    image_preview.short_description = 'Primary Image Preview'


@admin.register(models.ProductImage)
class ProductImageAdmin(admin.ModelAdmin):
    list_display = ('product', 'caption', 'display_order', 'image_preview', 'created_at')
    list_filter = ('product__tenant', 'product')
    search_fields = ('product__sku', 'product__product_name', 'caption')
    readonly_fields = ('image_preview',)
    list_editable = ('display_order',)  # Allow quick editing of display order

    def image_preview(self, obj):
        if obj.image:
            return f'<img src="{obj.image.url}" style="max-height: 50px; max-width: 50px;" />'
        return "No image"
    image_preview.allow_tags = True
    image_preview.short_description = 'Preview'



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


class EmployeeDocumentInline(admin.TabularInline):
    model = models.EmployeeDocument
    extra = 1
    readonly_fields = ('document_preview',)

    def document_preview(self, obj):
        if obj.document_file:
            file_extension = obj.document_file.name.split('.')[-1].lower()
            if file_extension in ['jpg', 'jpeg', 'png', 'webp']:
                return f'<img src="{obj.document_file.url}" style="max-height: 100px; max-width: 100px;" />'
            else:
                return f'<a href="{obj.document_file.url}" target="_blank">View Document</a>'
        return "No document"
    document_preview.allow_tags = True
    document_preview.short_description = 'Preview'


@admin.register(models.EmployeeDocument)
class EmployeeDocumentAdmin(admin.ModelAdmin):
    list_display = ('employee', 'document_type', 'document_name', 'expiry_date', 'created_at')
    list_filter = ('document_type', 'employee__tenant')
    search_fields = ('employee__employee_code', 'employee__full_name', 'document_name')
    readonly_fields = ('document_preview',)

    def document_preview(self, obj):
        if obj.document_file:
            file_extension = obj.document_file.name.split('.')[-1].lower()
            if file_extension in ['jpg', 'jpeg', 'png', 'webp']:
                return f'<img src="{obj.document_file.url}" style="max-height: 200px; max-width: 200px;" />'
            else:
                return f'<a href="{obj.document_file.url}" target="_blank">Download Document</a>'
        return "No document"
    document_preview.allow_tags = True
    document_preview.short_description = 'Document Preview'


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


@admin.register(models.ChartOfAccounts)
class ChartOfAccountsAdmin(admin.ModelAdmin):
    list_display = ('account_code', 'account_name', 'account_type', 'tenant', 'parent_account')
    list_filter = ('account_type', 'tenant')
    search_fields = ('account_code', 'account_name')


@admin.register(models.Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    list_display = ('equipment_code', 'equipment_name', 'tenant', 'location', 'acquisition_date')
    list_filter = ('tenant',)
    search_fields = ('equipment_code', 'equipment_name')


@admin.register(models.Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ('party_code', 'party_type', 'legal_name', 'display_name', 'tenant', 'gstin')
    list_filter = ('party_type', 'tenant')
    search_fields = ('party_code', 'legal_name', 'display_name')


class PurchaseOrderLineInline(admin.TabularInline):
    model = models.PurchaseOrderLine
    extra = 1
    readonly_fields = ('subtotal',)


@admin.register(models.PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    inlines = [PurchaseOrderLineInline]
    list_display = ('po_number', 'supplier', 'order_date', 'status', 'amount', 'has_document')
    list_filter = ('status', 'order_date', 'tenant')
    search_fields = ('po_number', 'supplier__display_name')
    readonly_fields = ('document_preview',)

    def has_document(self, obj):
        return bool(obj.po_document)
    has_document.boolean = True
    has_document.short_description = 'Has Document'

    def document_preview(self, obj):
        if obj.po_document:
            return f'<a href="{obj.po_document.url}" target="_blank">Download PO Document</a>'
        return "No document"
    document_preview.allow_tags = True
    document_preview.short_description = 'Document'


@admin.register(models.CustomerInvoice)
class CustomerInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'customer', 'invoice_date', 'due_date', 'invoice_amount', 'status', 'has_document')
    list_filter = ('status', 'invoice_date', 'tenant')
    search_fields = ('invoice_number', 'customer__display_name')
    readonly_fields = ('document_preview',)

    def has_document(self, obj):
        return bool(obj.invoice_document)
    has_document.boolean = True
    has_document.short_description = 'Has Document'

    def document_preview(self, obj):
        if obj.invoice_document:
            return f'<a href="{obj.invoice_document.url}" target="_blank">Download Invoice PDF</a>'
        return "No document"
    document_preview.allow_tags = True
    document_preview.short_description = 'Invoice Document'


@admin.register(models.CustomerPurchaseOrder)
class CustomerPurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ('po_number', 'customer', 'po_date', 'status', 'po_amount', 'has_document')
    list_filter = ('status', 'po_date', 'tenant')
    search_fields = ('po_number', 'customer__display_name')
    readonly_fields = ('document_preview',)

    def has_document(self, obj):
        return bool(obj.po_document)
    has_document.boolean = True
    has_document.short_description = 'Has Document'

    def document_preview(self, obj):
        if obj.po_document:
            return f'<a href="{obj.po_document.url}" target="_blank">Download Customer PO</a>'
        return "No document"
    document_preview.allow_tags = True
    document_preview.short_description = 'PO Document'


class PaymentAdviceInvoiceInline(admin.TabularInline):
    model = models.PaymentAdviceInvoice
    extra = 1


@admin.register(models.PaymentAdvice)
class PaymentAdviceAdmin(admin.ModelAdmin):
    inlines = [PaymentAdviceInvoiceInline]
    list_display = ('advice_number', 'customer', 'advice_date', 'total_payment_amount', 'has_document')
    list_filter = ('advice_date', 'tenant')
    search_fields = ('advice_number', 'customer__display_name')
    readonly_fields = ('document_preview',)

    def has_document(self, obj):
        return bool(obj.advice_document)
    has_document.boolean = True
    has_document.short_description = 'Has Document'

    def document_preview(self, obj):
        if obj.advice_document:
            file_extension = obj.advice_document.name.split('.')[-1].lower()
            if file_extension in ['jpg', 'jpeg', 'png', 'webp']:
                return f'<img src="{obj.advice_document.url}" style="max-height: 200px; max-width: 200px;" />'
            else:
                return f'<a href="{obj.advice_document.url}" target="_blank">Download Payment Advice</a>'
        return "No document"
    document_preview.allow_tags = True
    document_preview.short_description = 'Advice Document'


@admin.register(models.AIQueryLog)
class AIQueryLogAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'tenant', 'user_query', 'was_successful', 'execution_time_ms')
    list_filter = ('was_successful', 'tenant')
    search_fields = ('user_query',)
    readonly_fields = ('user_query', 'generated_sql', 'execution_time_ms', 'result_rows', 'was_successful', 'error_message')


@admin.register(models.AutomationRule)
class AutomationRuleAdmin(admin.ModelAdmin):
    list_display = ('rule_name', 'trigger_type', 'is_enabled', 'last_executed', 'tenant')
    list_filter = ('trigger_type', 'is_enabled', 'tenant')
    search_fields = ('rule_name',)


@admin.register(models.TenantEmailConfig)
class TenantEmailConfigAdmin(admin.ModelAdmin):
    list_display = ('tenant', 'weekly_report_enabled', 'send_day', 'send_time')
    list_filter = ('weekly_report_enabled', 'send_day', 'tenant')
    search_fields = ('tenant__company_name',)


@admin.register(models.GLJournalArchive)
class GLJournalArchiveAdmin(admin.ModelAdmin):
    list_display = ('journal_number', 'posting_date', 'tenant', 'total_debit', 'total_credit', 'status', 'archived_at')
    list_filter = ('status', 'posting_date', 'tenant')
    search_fields = ('journal_number', 'reference')
    readonly_fields = ('journal_number', 'posting_date', 'reference', 'narration', 'total_debit', 'total_credit', 'status', 'original_id', 'archived_at')


@admin.register(models.GLJournalLineArchive)
class GLJournalLineArchiveAdmin(admin.ModelAdmin):
    list_display = ('journal', 'line_number', 'account_code', 'debit_amount', 'credit_amount')
    list_filter = ('journal__tenant',)
    search_fields = ('journal__journal_number', 'account_code')
    readonly_fields = ('journal', 'line_number', 'account_code', 'cost_center_code', 'debit_amount', 'credit_amount', 'description', 'original_id')