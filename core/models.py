# models.py - Core ERP Models with File Storage

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, FileExtensionValidator
from django.utils import timezone
import os

# ===== FILE UPLOAD HELPERS =====
def get_product_image_path(instance, filename):
    """Generate upload path for product images"""
    ext = filename.split('.')[-1]
    
    # Handle both Product and ProductImage instances
    if hasattr(instance, 'sku'):
        # This is a Product instance (primary_image)
        sku = instance.sku
        tenant_id = instance.tenant.id
    else:
        # This is a ProductImage instance
        sku = instance.product.sku
        tenant_id = instance.product.tenant.id
    
    return f'tenants/{tenant_id}/products/{sku}/images/{filename}'

def get_employee_document_path(instance, filename):
    """Generate upload path for employee documents"""
    return f'tenants/{instance.employee.tenant.id}/employees/{instance.employee.employee_code}/docs/{filename}'

def get_po_document_path(instance, filename):
    """Generate upload path for purchase order documents"""
    return f'tenants/{instance.tenant.id}/purchase_orders/{instance.po_number}/{filename}'

def get_invoice_document_path(instance, filename):
    """Generate upload path for customer invoices"""
    return f'tenants/{instance.tenant.id}/invoices/{instance.invoice_number}/{filename}'

def get_payment_advice_path(instance, filename):
    """Generate upload path for payment advices"""
    return f'tenants/{instance.tenant.id}/payment_advices/{instance.advice_number}/{filename}'

def get_customer_po_path(instance, filename):
    """Generate upload path for customer POs"""
    return f'tenants/{instance.tenant.id}/customer_pos/{instance.customer.party_code}/{filename}'

# ===== TENANT MANAGEMENT =====
class Tenant(models.Model):
    """Multi-tenant organization management"""
    PLAN_CHOICES = [
        ('basic', 'Basic Plan'),
        ('professional', 'Professional Plan'),
        ('enterprise', 'Enterprise Plan')
    ]
    
    company_name = models.CharField(max_length=200)
    subdomain = models.CharField(max_length=50, unique=True)
    plan_type = models.CharField(max_length=20, choices=PLAN_CHOICES, default='basic')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    gstin = models.CharField(max_length=15, blank=True)
    company_address = models.TextField(blank=True, default="")  
    modules_enabled = models.JSONField(default=dict)
    
    def __str__(self):
        return self.company_name

class TenantUser(models.Model):
    """User-Tenant relationship with roles"""
    ROLE_CHOICES = [
        ('admin', 'Administrator'),
        ('manager', 'Manager'),
        ('operator', 'Operator'),
        ('viewer', 'Viewer')
    ]
    
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['tenant', 'user']

# ===== MASTER DATA MANAGEMENT =====
class BaseModel(models.Model):
    """Abstract base model with tenant isolation and audit"""
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        abstract = True

class ChartOfAccounts(BaseModel):
    """Chart of accounts for financial management"""
    ACCOUNT_TYPES = [
        ('asset', 'Asset'),
        ('liability', 'Liability'),
        ('equity', 'Equity'),
        ('revenue', 'Revenue'),
        ('expense', 'Expense'),
        ('cogs', 'Cost of Goods Sold')
    ]
    
    account_code = models.CharField(max_length=20)
    account_name = models.CharField(max_length=200)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    parent_account = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE)
    
    class Meta:
        unique_together = ['tenant', 'account_code']
        ordering = ['account_code']
    
    def __str__(self):
        return f"{self.account_code} - {self.account_name}"

class CostCenter(BaseModel):
    """Cost centers for tracking departmental costs"""
    cost_center_code = models.CharField(max_length=20)
    name = models.CharField(max_length=200)
    parent_center = models.ForeignKey('self', null=True, blank=True, on_delete=models.CASCADE)
    manager = models.ForeignKey(
        'Employee',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="manages_cost_centers"
    )
    
    class Meta:
        unique_together = ['tenant', 'cost_center_code']
    
    def __str__(self):
        return f"{self.cost_center_code} - {self.name}"

class Product(BaseModel):
    """Master product catalog with image support"""
    PRODUCT_TYPES = [
        ('raw_material', 'Raw Material'),
        ('finished_good', 'Finished Good'),
        ('semi_finished', 'Semi-Finished'),
        ('consumable', 'Consumable')
    ]
    
    UOM_CHOICES = [
        ('pcs', 'Pieces'),
        ('kg', 'Kilograms'),
        ('ltr', 'Liters'),
        ('mtr', 'Meters'),
        ('set', 'Set')
    ]
    
    sku = models.CharField(max_length=50)
    product_name = models.CharField(max_length=200)
    product_type = models.CharField(max_length=20, choices=PRODUCT_TYPES)
    uom = models.CharField(max_length=10, choices=UOM_CHOICES)
    category = models.CharField(max_length=100)
    standard_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    reorder_point = models.IntegerField(default=0)
    specifications = models.TextField(null=True, blank=True, default='')
    
    # PRIMARY IMAGE
    primary_image = models.ImageField(
        upload_to=get_product_image_path,
        null=True,
        blank=True,
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])],
        help_text="Main product image (JPG, PNG, WEBP)"
    )
    
    class Meta:
        unique_together = ['tenant', 'sku']
    
    def __str__(self):
        return f"{self.sku} - {self.product_name}"
    
    def delete(self, *args, **kwargs):
        """Delete associated files when product is deleted"""
        if self.primary_image:
            if os.path.isfile(self.primary_image.path):
                os.remove(self.primary_image.path)
        super().delete(*args, **kwargs)

class ProductImage(BaseModel):
    """Additional product images (optional - for multiple images)"""
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='additional_images')
    image = models.ImageField(
        upload_to=get_product_image_path,
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])]
    )
    caption = models.CharField(max_length=200, blank=True)
    display_order = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['display_order', 'created_at']
    
    def __str__(self):
        return f"{self.product.sku} - Image {self.id}"
    
    def delete(self, *args, **kwargs):
        if self.image:
            if os.path.isfile(self.image.path):
                os.remove(self.image.path)
        super().delete(*args, **kwargs)

class Party(BaseModel):
    """Master party record - customers, suppliers, others"""
    PARTY_TYPES = [
        ('customer', 'Customer'),
        ('supplier', 'Supplier'),
        ('other', 'Other')
    ]
    
    party_code = models.CharField(max_length=30)
    party_type = models.CharField(max_length=20, choices=PARTY_TYPES)
    legal_name = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=15, blank=True)
    pan = models.CharField(max_length=10, blank=True)
    contact_details = models.JSONField(default=dict)
    payment_terms = models.IntegerField(default=30)
    credit_limit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    class Meta:
        unique_together = ['tenant', 'party_code']
        verbose_name_plural = 'Parties'
    
    def __str__(self):
        return f"{self.party_code} - {self.display_name}"

# ===== PRODUCTION MODULE =====
class Equipment(BaseModel):
    """Manufacturing equipment/machines"""
    equipment_code = models.CharField(max_length=30)
    equipment_name = models.CharField(max_length=200)
    location = models.CharField(max_length=100)
    capacity_per_hour = models.IntegerField(default=0)
    acquisition_date = models.DateField()
    last_maintenance = models.DateTimeField(null=True, blank=True)
    next_maintenance = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['tenant', 'equipment_code']
    
    def __str__(self):
        return f"{self.equipment_code} - {self.equipment_name}"

class Employee(BaseModel):
    """Employee master with production/HR focus"""
    employee_code = models.CharField(max_length=20)
    full_name = models.CharField(max_length=200)
    department = models.CharField(max_length=100)
    designation = models.CharField(max_length=100)
    cost_center = models.ForeignKey(
        CostCenter,
        on_delete=models.CASCADE,
        related_name="employees"
    )
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    skill_level = models.IntegerField(default=1, validators=[MinValueValidator(1)])
    hire_date = models.DateField()
    
    class Meta:
        unique_together = ['tenant', 'employee_code']
    
    def __str__(self):
        return f"{self.employee_code} - {self.full_name}"

class EmployeeDocument(BaseModel):
    """Employee documents - ID proofs, certificates, etc."""
    DOCUMENT_TYPES = [
        ('id_proof', 'ID Proof'),
        ('address_proof', 'Address Proof'),
        ('educational', 'Educational Certificate'),
        ('experience', 'Experience Letter'),
        ('other', 'Other')
    ]
    
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=20, choices=DOCUMENT_TYPES)
    document_name = models.CharField(max_length=200)
    document_file = models.FileField(
        upload_to=get_employee_document_path,
        validators=[FileExtensionValidator(['pdf', 'jpg', 'jpeg', 'png'])],
        help_text="Upload PDF or Image (JPG, PNG)"
    )
    expiry_date = models.DateField(null=True, blank=True, help_text="For documents like ID cards")
    notes = models.TextField(blank=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.employee.employee_code} - {self.document_name}"
    
    def delete(self, *args, **kwargs):
        if self.document_file:
            if os.path.isfile(self.document_file.path):
                os.remove(self.document_file.path)
        super().delete(*args, **kwargs)

class WorkOrder(BaseModel):
    """Production work orders"""
    STATUS_CHOICES = [
        ('planned', 'Planned'),
        ('released', 'Released'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled')
    ]
    
    wo_number = models.CharField(max_length=30)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity_planned = models.IntegerField()
    quantity_completed = models.IntegerField(default=0)
    quantity_scrapped = models.IntegerField(default=0)
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='planned')
    cost_center = models.ForeignKey(CostCenter, on_delete=models.CASCADE)
    priority = models.IntegerField(default=5)
    description = models.TextField(blank=True, null=True)
    
    class Meta:
        unique_together = ['tenant', 'wo_number']
    
    def __str__(self):
        return f"{self.wo_number} - {self.product.sku}"
    
    @property
    def completion_percentage(self):
        if self.quantity_planned > 0:
            return (self.quantity_completed / self.quantity_planned) * 100
        return 0
        
class ProductionEntry(BaseModel):
    """Hourly production recording"""
    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE)
    operator = models.ForeignKey(Employee, on_delete=models.CASCADE)
    entry_datetime = models.DateTimeField()
    quantity_produced = models.IntegerField()
    quantity_rejected = models.IntegerField(default=0)
    downtime_minutes = models.IntegerField(default=0)
    downtime_reason = models.CharField(max_length=200, blank=True)
    shift = models.CharField(max_length=20)
    
    def __str__(self):
        return f"{self.work_order.wo_number} - {self.entry_datetime}"

# ===== INVENTORY MODULE =====
class Warehouse(BaseModel):
    """Warehouse/storage locations"""
    warehouse_code = models.CharField(max_length=20)
    warehouse_name = models.CharField(max_length=200)
    location = models.CharField(max_length=200)
    manager = models.ForeignKey(Employee, null=True, blank=True, on_delete=models.SET_NULL)
    
    class Meta:
        unique_together = ['tenant', 'warehouse_code']
    
    def __str__(self):
        return f"{self.warehouse_code} - {self.warehouse_name}"

class StockMovement(BaseModel):
    """All inventory movements"""
    MOVEMENT_TYPES = [
        ('receipt', 'Receipt'),
        ('issue', 'Issue'),
        ('transfer_in', 'Transfer In'),
        ('transfer_out', 'Transfer Out'),
        ('adjustment', 'Adjustment'),
        ('production_receipt', 'Production Receipt'),
        ('production_issue', 'Production Issue')
    ]
    
    movement_number = models.CharField(max_length=30)
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    warehouse = models.ForeignKey(Warehouse, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    reference_doc = models.CharField(max_length=50, blank=True)
    movement_date = models.DateTimeField()
    
    class Meta:
        unique_together = ['tenant', 'movement_number']
    
    def __str__(self):
        return f"{self.movement_number} - {self.movement_type}"

# ===== FINANCIAL MODULE =====
class GLJournal(BaseModel):
    """General ledger journal headers"""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('posted', 'Posted'),
        ('cancelled', 'Cancelled')
    ]
    
    journal_number = models.CharField(max_length=30)
    posting_date = models.DateField()
    reference = models.CharField(max_length=100, blank=True)
    narration = models.TextField(blank=True)
    total_debit = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_credit = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    
    class Meta:
        unique_together = ['tenant', 'journal_number']
    
    def __str__(self):
        return f"{self.journal_number} - {self.posting_date}"

class GLJournalLine(BaseModel):
    """General ledger journal lines"""
    journal = models.ForeignKey(GLJournal, on_delete=models.CASCADE, related_name='lines')
    line_number = models.IntegerField()
    account = models.ForeignKey(ChartOfAccounts, on_delete=models.CASCADE)
    cost_center = models.ForeignKey(CostCenter, null=True, blank=True, on_delete=models.CASCADE)
    debit_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    description = models.CharField(max_length=200, blank=True)
    
    class Meta:
        unique_together = ['tenant', 'journal', 'line_number']

# ===== PURCHASE ORDERS =====
class PurchaseOrder(BaseModel):
    """Purchase Order management with document attachment"""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('received', 'Received'),
        ('cancelled', 'Cancelled')
    ]
    
    po_number = models.CharField(max_length=30)
    supplier = models.ForeignKey(
        Party, 
        on_delete=models.CASCADE, 
        limit_choices_to={'party_type': 'supplier'},
        related_name='purchase_orders'
    )
    order_date = models.DateField(default=timezone.now)
    expected_delivery = models.DateField(null=True, blank=True)
    delivery_address = models.TextField(blank=True)
    terms_conditions = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # DOCUMENT ATTACHMENT
    po_document = models.FileField(
        upload_to=get_po_document_path,
        null=True,
        blank=True,
        validators=[FileExtensionValidator(['pdf', 'docx', 'xlsx'])],
        help_text="Upload PO document (PDF, DOCX, XLSX)"
    )
    
    class Meta:
        unique_together = ['tenant', 'po_number']
        ordering = ['-order_date']
    
    def __str__(self):
        return f"{self.po_number} - {self.supplier.display_name}"
    
    def save(self, *args, **kwargs):
        if self.pk:
            self.amount = sum(line.subtotal for line in self.lines.all())
        super().save(*args, **kwargs)
    
    def delete(self, *args, **kwargs):
        if self.po_document:
            if os.path.isfile(self.po_document.path):
                os.remove(self.po_document.path)
        super().delete(*args, **kwargs)

class PurchaseOrderLine(BaseModel):
    """Line items for Purchase Orders"""
    purchase_order = models.ForeignKey(
        PurchaseOrder, 
        on_delete=models.CASCADE, 
        related_name='lines'
    )
    line_number = models.IntegerField()
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    
    class Meta:
        unique_together = ['purchase_order', 'line_number']
        ordering = ['line_number']
    
    def save(self, *args, **kwargs):
        self.subtotal = self.quantity * self.unit_price
        super().save(*args, **kwargs)

# ===== CUSTOMER INVOICES =====
class CustomerInvoice(BaseModel):
    """Customer invoices with document attachment"""
    STATUS_CHOICES = [
        ('sent', 'Sent'),
        ('paid', 'Paid'),
        ('partial_paid', 'Partially Paid'),
        ('overdue', 'Overdue'),
        ('cancelled', 'Cancelled')
    ]
    
    invoice_number = models.CharField(max_length=50)
    customer = models.ForeignKey(
        Party, 
        on_delete=models.CASCADE,
        limit_choices_to={'party_type': 'customer'},
        related_name='invoices'
    )
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    invoice_amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='sent')
    reference_customer_po = models.ForeignKey(
        'CustomerPurchaseOrder', 
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='invoices'
    )
    notes = models.TextField(blank=True)
    
    # DOCUMENT ATTACHMENT
    invoice_document = models.FileField(
        upload_to=get_invoice_document_path,
        null=True,
        blank=True,
        validators=[FileExtensionValidator(['pdf'])],
        help_text="Upload invoice PDF"
    )
    
    class Meta:
        unique_together = ['tenant', 'invoice_number']
        ordering = ['-invoice_date']
    
    def __str__(self):
        return f"{self.invoice_number} - {self.customer.display_name}"
    
    def delete(self, *args, **kwargs):
        """
        Delete the attached file using the storage API, then delete the DB row.
        This avoids direct filesystem assumptions (works with S3, remote storages).
        """
        if self.invoice_document:
            try:
                self.invoice_document.delete(save=False)
            except Exception:
                # keep deletion best-effort; don't fail the DB delete if file deletion fails
                pass
        super().delete(*args, **kwargs)

class CustomerPurchaseOrder(BaseModel):
    """Customer POs received with document attachment"""
    STATUS_CHOICES = [
        ('received', 'Received'),
        ('acknowledged', 'Acknowledged'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled')
    ]
    
    po_number = models.CharField(max_length=50)
    customer = models.ForeignKey(
        Party,
        on_delete=models.CASCADE,
        limit_choices_to={'party_type': 'customer'},
        related_name='customer_purchase_orders'
    )
    po_date = models.DateField()
    delivery_required_by = models.DateField(null=True, blank=True)
    po_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='received')
    description = models.TextField(blank=True)
    special_instructions = models.TextField(blank=True)
    
    # DOCUMENT ATTACHMENT
    po_document = models.FileField(
        upload_to=get_customer_po_path,
        null=True,
        blank=True,
        validators=[FileExtensionValidator(['pdf', 'docx', 'xlsx'])],
        help_text="Upload customer PO document"
    )
    
    class Meta:
        unique_together = ['tenant', 'customer', 'po_number']
        ordering = ['-po_date']
    
    def __str__(self):
        return f"Customer PO: {self.po_number} - {self.customer.display_name}"
    
    def save(self, *args, **kwargs):
        """Ensure is_active is True when creating new records"""
        if not self.pk:  # Only for new instances
            self.is_active = True
        super().save(*args, **kwargs)
    
    def delete(self, *args, **kwargs):
        """Safe file deletion using storage API"""
        if self.po_document:
            try:
                self.po_document.delete(save=False)
            except Exception:
                # Keep deletion best-effort
                pass
        super().delete(*args, **kwargs)
        
class PaymentAdvice(BaseModel):
    """Payment advices from customers with document attachment"""
    advice_number = models.CharField(max_length=50, blank=True)
    customer = models.ForeignKey(
        Party,
        on_delete=models.CASCADE,
        limit_choices_to={'party_type': 'customer'},
        related_name='payment_advices'
    )
    advice_date = models.DateField()
    total_payment_amount = models.DecimalField(max_digits=15, decimal_places=2)
    mentioned_invoices = models.ManyToManyField(
        CustomerInvoice,
        through='PaymentAdviceInvoice',
        related_name='payment_advices'
    )
    notes = models.TextField(blank=True)
    
    # DOCUMENT ATTACHMENT
    advice_document = models.FileField(
        upload_to=get_payment_advice_path,
        null=True,
        blank=True,
        validators=[FileExtensionValidator(['pdf', 'jpg', 'jpeg', 'png'])],
        help_text="Upload payment advice document"
    )
    
    class Meta:
        ordering = ['-advice_date']
    
    def __str__(self):
        return f"Payment Advice {self.advice_number} - {self.customer.display_name}"
    
    def delete(self, *args, **kwargs):
        if self.advice_document:
            if os.path.isfile(self.advice_document.path):
                os.remove(self.advice_document.path)
        super().delete(*args, **kwargs)

class PaymentAdviceInvoice(BaseModel):
    """Track invoices mentioned in payment advice"""
    payment_advice = models.ForeignKey(PaymentAdvice, on_delete=models.CASCADE)
    invoice = models.ForeignKey(CustomerInvoice, on_delete=models.CASCADE)
    amount_mentioned = models.DecimalField(max_digits=12, decimal_places=2)
    
    class Meta:
        unique_together = ['payment_advice', 'invoice']
    
    def __str__(self):
        return f"{self.payment_advice.advice_number} - {self.invoice.invoice_number}"

# ===== AI & SYSTEM =====
class AIQueryLog(BaseModel):
    """Log all AI queries for learning and audit"""
    user_query = models.TextField()
    generated_sql = models.TextField(blank=True)
    execution_time_ms = models.IntegerField(default=0)
    result_rows = models.IntegerField(default=0)
    was_successful = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    
    def __str__(self):
        return f"Query at {self.created_at}"

class AutomationRule(BaseModel):
    """System automation rules"""
    TRIGGER_TYPES = [
        ('time_based', 'Time Based'),
        ('event_based', 'Event Based'),
        ('threshold_based', 'Threshold Based')
    ]
    
    rule_name = models.CharField(max_length=200)
    trigger_type = models.CharField(max_length=20, choices=TRIGGER_TYPES)
    trigger_condition = models.JSONField(default=dict)
    action_definition = models.JSONField(default=dict)
    is_enabled = models.BooleanField(default=True)
    last_executed = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return self.rule_name

class TenantEmailConfig(BaseModel):
    """Email preferences for weekly reports"""
    tenant = models.ForeignKey('Tenant', on_delete=models.CASCADE, related_name='email_configs')
    weekly_report_enabled = models.BooleanField(default=True)
    recipients = models.JSONField(default=list)
    send_day = models.CharField(max_length=10, default='sunday', choices=[('monday', 'Monday'), ('sunday', 'Sunday')])
    send_time = models.TimeField(default='08:00:00')
    
    class Meta:
        unique_together = ['tenant']

# ===== ARCHIVES =====
class GLJournalArchive(BaseModel):
    """Archived GL Journals"""
    journal_number = models.CharField(max_length=30)
    posting_date = models.DateField()
    reference = models.CharField(max_length=255, blank=True)
    narration = models.TextField(blank=True)
    total_debit = models.DecimalField(max_digits=15, decimal_places=2)
    total_credit = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20)
    original_id = models.IntegerField()
    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['tenant', 'journal_number']),
            models.Index(fields=['posting_date'])
        ]

class GLJournalLineArchive(BaseModel):
    """Archived GL Journal Lines"""
    journal = models.ForeignKey(GLJournalArchive, on_delete=models.CASCADE, related_name='lines')
    line_number = models.IntegerField()
    account_code = models.CharField(max_length=20)
    cost_center_code = models.CharField(max_length=20, blank=True)
    debit_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    credit_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    description = models.TextField(blank=True)
    original_id = models.IntegerField()

    class Meta:
        indexes = [
            models.Index(fields=['journal', 'line_number'])
        ]