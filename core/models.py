# models.py - Core ERP Models

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import MinValueValidator
import json

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
    modules_enabled = models.JSONField(default=dict)  # {"production": True, "finance": True}
    
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

    # manager will be linked after Employee is created
    manager = models.ForeignKey(
        'Employee',  # string reference is fine
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="manages_cost_centers"
    )
    
    class Meta:
        unique_together = ['tenant', 'cost_center_code']
    
    def __str__(self):
        return f"{self.cost_center_code} - {self.name}"

class Product(BaseModel):
    """Master product catalog"""
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
    standard_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)  # Fixed decimal default
    reorder_point = models.IntegerField(default=0)
    specifications = models.TextField(null=True, blank=True, default='')  # Changed to TextField
    
    class Meta:
        unique_together = ['tenant', 'sku']
    
    def __str__(self):
        return f"{self.sku} - {self.product_name}"

class Party(BaseModel):
    """Master party record - customers, suppliers, others (not employees)"""
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
    contact_details = models.JSONField(default=dict)  # emails, phones, addresses
    payment_terms = models.IntegerField(default=30)  # days
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
    priority = models.IntegerField(default=5)  # 1=High, 10=Low
    
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
    shift = models.CharField(max_length=20)  # A, B, C, General
    
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
    """All inventory movements (receipts, issues, transfers)"""
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
    reference_doc = models.CharField(max_length=50, blank=True)  # WO number, PO number, etc.
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

# ===== AI PREPARATION =====
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

# ===== SYSTEM AUTOMATION =====
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