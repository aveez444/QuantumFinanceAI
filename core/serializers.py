# core/serializers.py - Complete ERP Serializers

from rest_framework import serializers
from django.contrib.auth.models import User
from decimal import Decimal
from django.db.models import Sum, F
from django.contrib.auth import authenticate
from .middleware import get_current_tenant
from .models import (
    Tenant, TenantUser, Product, ProductImage, WorkOrder, ProductionEntry, 
    Equipment, Employee, EmployeeDocument, StockMovement, ChartOfAccounts,
    GLJournal, GLJournalLine, CostCenter, Warehouse, Party, 
    PurchaseOrder, PurchaseOrderLine, CustomerInvoice, 
    CustomerPurchaseOrder, PaymentAdvice, PaymentAdviceInvoice
)
from django.utils import timezone

# ===== AUTHENTICATION SERIALIZERS =====

class TenantWithAdminSerializer(serializers.Serializer):
    """Superuser creates a tenant + its first admin user"""
    company_name = serializers.CharField(max_length=200)
    subdomain = serializers.CharField(max_length=50)
    plan_type = serializers.ChoiceField(choices=['basic', 'professional', 'enterprise'], default='basic')
    
    # First admin credentials
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)

    def validate_subdomain(self, value):
        """Ensure subdomain is unique"""
        if Tenant.objects.filter(subdomain=value).exists():
            raise serializers.ValidationError("Subdomain already exists")
        return value

    def create(self, validated_data):
        # Extract tenant fields
        company_name = validated_data['company_name']
        subdomain = validated_data['subdomain']
        plan_type = validated_data['plan_type']

        # Create tenant
        tenant = Tenant.objects.create(
            company_name=company_name,
            subdomain=subdomain,
            plan_type=plan_type,
            modules_enabled={
                'production': True,
                'inventory': True,
                'finance': plan_type != 'basic',
                'procurement': True
            }
        )

        # Create first admin user
        user = User.objects.create_user(
            username=validated_data['username'],
            email=validated_data['email'],
            password=validated_data['password']
        )

        # Link user to tenant
        TenantUser.objects.create(
            tenant=tenant,
            user=user,
            role='admin'
        )

        return tenant, user

class LoginSerializer(serializers.Serializer):
    """User login with tenant context"""
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)
    subdomain = serializers.CharField(required=False)

    def validate(self, attrs):
        user = authenticate(
            username=attrs.get("username"),
            password=attrs.get("password")
        )
        if not user:
            raise serializers.ValidationError("Invalid credentials")
        
        # Validate user has access to tenant
        subdomain = attrs.get('subdomain')
        if subdomain:
            try:
                tenant = Tenant.objects.get(subdomain=subdomain, is_active=True)
                tenant_user = TenantUser.objects.get(tenant=tenant, user=user, is_active=True)
                attrs['tenant'] = tenant
                attrs['tenant_role'] = tenant_user.role
            except (Tenant.DoesNotExist, TenantUser.DoesNotExist):
                raise serializers.ValidationError("User not authorized for this tenant")
        
        attrs['user'] = user    
        return attrs

# ===== MASTER DATA SERIALIZERS =====


class ProductSerializer(serializers.ModelSerializer):
    """Product master serializer with stock info"""
    current_stock = serializers.SerializerMethodField()
    stock_value = serializers.SerializerMethodField()
    
    class Meta:
        model = Product
        fields = [
            'id', 'sku', 'product_name', 'product_type', 'uom', 
            'category', 'standard_cost', 'reorder_point', 'specifications', 'primary_image',
            'current_stock', 'stock_value', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'current_stock', 'stock_value']
    
    def get_current_stock(self, obj):
        """Calculate current stock from movements"""
        from django.db.models import Sum
        stock = StockMovement.objects.filter(
            tenant=obj.tenant,
            product=obj
        ).aggregate(Sum('quantity'))['quantity__sum']
        # Return as Decimal instead of float
        return Decimal(str(stock)) if stock else Decimal('0')
    
    def get_stock_value(self, obj):
        """Calculate current stock value"""
        current_stock = self.get_current_stock(obj)
        standard_cost = obj.standard_cost if obj.standard_cost else Decimal('0')
        # Both are Decimal now, so multiplication works
        return current_stock * standard_cost

class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 'image', 'caption', 'display_order', 'created_at']
        read_only_fields = ['id', 'created_at']

class EmployeeDocumentSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(source='employee.full_name', read_only=True)
    
    class Meta:
        model = EmployeeDocument
        fields = ['id', 'employee', 'employee_name', 'document_type', 'document_name', 
                  'document_file', 'expiry_date', 'notes', 'created_at']
        read_only_fields = ['id', 'created_at']


class PartySerializer(serializers.ModelSerializer):
    """Customer/Supplier master serializer"""
    outstanding_balance = serializers.SerializerMethodField()
    
    class Meta:
        model = Party
        fields = [
            'id', 'party_code', 'party_type', 'legal_name', 'display_name',
            'gstin', 'pan', 'contact_details', 'payment_terms', 'credit_limit',
            'outstanding_balance', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'outstanding_balance']
    
    def get_outstanding_balance(self, obj):
        """Calculate outstanding balance (simplified)"""
        # This would integrate with AR/AP modules when implemented
        return 0.0

class EquipmentSerializer(serializers.ModelSerializer):
    """Equipment master with maintenance status"""
    maintenance_status = serializers.SerializerMethodField()
    current_oee = serializers.SerializerMethodField()
    
    class Meta:
        model = Equipment
        fields = [
            'id', 'equipment_code', 'equipment_name', 'location',
            'capacity_per_hour', 'acquisition_date', 'last_maintenance',
            'next_maintenance', 'maintenance_status', 'current_oee',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'maintenance_status', 'current_oee']
    
    def get_maintenance_status(self, obj):
        """Check maintenance status"""
        from django.utils import timezone
        if obj.next_maintenance:
            if obj.next_maintenance < timezone.now():
                return 'OVERDUE'
            elif obj.next_maintenance < timezone.now() + timezone.timedelta(days=7):
                return 'DUE_SOON'
        return 'OK'
    
    def get_current_oee(self, obj):
        """Get latest OEE for this equipment"""
        from django.core.cache import cache
        cache_key = f"oee_{obj.tenant.id}_{obj.id}"
        oee_data = cache.get(cache_key, {'oee': 0})
        return oee_data.get('oee', 0)

class EmployeeSerializer(serializers.ModelSerializer):
    """Employee master with productivity metrics"""
    cost_center_name = serializers.CharField(source='cost_center.name', read_only=True)
    recent_productivity = serializers.SerializerMethodField()
    
    class Meta:
        model = Employee
        fields = [
            'id', 'employee_code', 'full_name', 'department', 'designation',
            'cost_center', 'cost_center_name', 'hourly_rate', 'skill_level',
            'hire_date', 'recent_productivity', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'cost_center_name', 'recent_productivity']
    
    def get_recent_productivity(self, obj):
        """Get ALL productivity metrics for the employee"""
        from django.db.models import Sum
        
        # Get ALL production entries for this employee
        all_entries = ProductionEntry.objects.filter(operator=obj)
        
        if not all_entries.exists():
            return {'hours_worked': 0, 'total_produced': 0, 'quality_rate': 0, 'avg_hourly_output': 0}
        
        total_produced = all_entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
        total_rejected = all_entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
        hours_worked = all_entries.count()
        
        quality_rate = (total_produced / max(total_produced + total_rejected, 1)) * 100
        
        return {
            'hours_worked': hours_worked,
            'total_produced': total_produced,
            'total_rejected': total_rejected,
            'quality_rate': round(quality_rate, 2),
            'avg_hourly_output': round(total_produced / max(hours_worked, 1), 2)
        }

class CostCenterSerializer(serializers.ModelSerializer):
    """Cost center with employee count"""
    employee_count = serializers.SerializerMethodField()
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    
    class Meta:
        model = CostCenter
        fields = [
            'id', 'cost_center_code', 'name', 'parent_center', 
            'manager', 'manager_name', 'employee_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'manager_name', 'employee_count']
    
    def get_employee_count(self, obj):
        """Count active employees in this cost center"""
        return obj.employees.filter(is_active=True).count()

# ===== PRODUCTION SERIALIZERS =====

class WorkOrderSerializer(serializers.ModelSerializer):
    """Work order with progress tracking"""
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    cost_center_name = serializers.CharField(source='cost_center.name', read_only=True)
    completion_percentage = serializers.SerializerMethodField(read_only=True)
    efficiency_rate = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkOrder
        fields = [
            'id', 'wo_number', 'product', 'product_name', 'product_sku',
            'quantity_planned', 'quantity_completed', 'quantity_scrapped',
            'due_date', 'status', 'cost_center', 'cost_center_name', 'priority',
            'description',        
            'completion_percentage', 'efficiency_rate',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'wo_number', 'completion_percentage', 'created_at', 'updated_at']

    def get_completion_percentage(self, obj):
        if obj.quantity_planned and obj.quantity_planned > 0:
            return round((obj.quantity_completed / obj.quantity_planned) * 100, 2)
        return 0.0

    def get_efficiency_rate(self, obj):
        """Calculate current efficiency rate"""
        try:
            if not obj.quantity_planned or obj.quantity_planned <= 0:
                return 0.0

            # Ensure created_at is a date for subtraction
            created_date = obj.created_at.date() if hasattr(obj.created_at, 'date') else obj.created_at
            if obj.due_date is None:
                return 0.0

            days_elapsed = (timezone.now().date() - created_date).days + 1
            days_planned = (obj.due_date - created_date).days + 1

            # protect against zero or negative
            days_elapsed = max(days_elapsed, 1)
            days_planned = max(days_planned, 1)

            time_efficiency = (days_planned / days_elapsed) * 100
            qty_efficiency = (obj.quantity_completed / obj.quantity_planned) * 100

            return round(min(time_efficiency, qty_efficiency), 2)
        except Exception:
            return 0.0

class ProductionEntrySerializer(serializers.ModelSerializer):
    """Production entry with validation"""
    work_order_number = serializers.CharField(source='work_order.wo_number', read_only=True)
    equipment_name = serializers.CharField(source='equipment.equipment_name', read_only=True)
    operator_name = serializers.CharField(source='operator.full_name', read_only=True)
    efficiency_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = ProductionEntry
        fields = [
            'id', 'work_order', 'work_order_number', 'equipment', 'equipment_name',
            'operator', 'operator_name', 'entry_datetime', 'quantity_produced',
            'quantity_rejected', 'downtime_minutes', 'downtime_reason',
            'shift', 'efficiency_percentage', 'created_at'
        ]
        read_only_fields = ['id', 'created_at', 'efficiency_percentage']
    
    def get_efficiency_percentage(self, obj):
        """Calculate efficiency for this entry"""
        if obj.equipment.capacity_per_hour > 0:
            efficiency = (obj.quantity_produced / obj.equipment.capacity_per_hour) * 100
            return round(min(efficiency, 100), 2)
        return 0
    
    def validate(self, data):
        """Validate production entry data"""
        # Check if work order is active
        if data['work_order'].status not in ['released', 'in_progress']:
            raise serializers.ValidationError("Cannot add entries to inactive work orders")
        
        # NOTE: Capacity check removed as requested.
        # Previously we had a check that compared produced+rejected against equipment capacity * 1.2.
        # That logic has been intentionally removed so higher outputs won't be rejected automatically.
        
        return data

class StockMovementSerializer(serializers.ModelSerializer):
    """Stock movement with validation"""
    product_name = serializers.CharField(source='product.product_name', read_only=True)
    product_sku = serializers.CharField(source='product.sku', read_only=True)
    warehouse_name = serializers.CharField(source='warehouse.warehouse_name', read_only=True)
    movement_value = serializers.SerializerMethodField()
    
    class Meta:
        model = StockMovement
        fields = [
            'id', 'movement_number', 'movement_type', 'product', 'product_name', 
            'product_sku', 'warehouse', 'warehouse_name', 'quantity', 'unit_cost',
            'reference_doc', 'movement_date', 'movement_value', 'created_at'
        ]
        read_only_fields = ['id', 'movement_number', 'movement_value', 'created_at']
    
    def get_movement_value(self, obj):
        """Calculate movement value"""
        return float(abs(obj.quantity) * obj.unit_cost)
    
    def validate(self, data):
        """Validate stock movement"""
        # For outgoing movements, check stock availability
        if data['movement_type'] in ['issue', 'transfer_out', 'production_issue']:
            if data['quantity'] > 0:
                raise serializers.ValidationError("Outgoing movements should have negative quantity")
            
            # Check available stock
            from django.db.models import Sum
            available_stock = StockMovement.objects.filter(
                tenant=self.context['request'].user.tenantuser_set.first().tenant,
                product=data['product'],
                warehouse=data['warehouse']
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            if available_stock < abs(data['quantity']):
                raise serializers.ValidationError(
                    f"Insufficient stock. Available: {available_stock}, Required: {abs(data['quantity'])}"
                )
        
        return data

class WarehouseSerializer(serializers.ModelSerializer):
    """Warehouse master"""
    manager_name = serializers.CharField(source='manager.full_name', read_only=True)
    total_stock_value = serializers.SerializerMethodField()
    
    class Meta:
        model = Warehouse
        fields = [
            'id', 'warehouse_code', 'warehouse_name', 'location',
            'manager', 'manager_name', 'total_stock_value',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'total_stock_value']
    
    def get_total_stock_value(self, obj):
        """Calculate total stock value in warehouse"""
        from django.db.models import Sum, F
        total_value = StockMovement.objects.filter(
            warehouse=obj
        ).aggregate(
            total=Sum(F('quantity') * F('unit_cost'))
        )['total']
        return float(total_value) if total_value else 0.0

# ===== FINANCIAL SERIALIZERS =====

class ChartOfAccountsSerializer(serializers.ModelSerializer):
    """Chart of accounts with balance"""
    current_balance = serializers.SerializerMethodField()
    parent_account_name = serializers.CharField(source='parent_account.account_name', read_only=True, allow_null=True)
    
    class Meta:
        model = ChartOfAccounts
        fields = [
            'id', 'account_code', 'account_name', 'account_type',
            'parent_account', 'parent_account_name', 'current_balance',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'current_balance']
    
    def get_current_balance(self, obj):
        """Calculate current account balance"""
        from django.db.models import Sum
        balance = GLJournalLine.objects.filter(
            account=obj,
            journal__status='posted'
        ).aggregate(
            balance=Sum(F('debit_amount') - F('credit_amount'))
        )['balance']
        return float(balance) if balance else 0.0

class GLJournalLineSerializer(serializers.ModelSerializer):
    """GL Journal line with account details"""
    account_name = serializers.CharField(source='account.account_name', read_only=True)
    account_code = serializers.CharField(source='account.account_code', read_only=True)
    cost_center_name = serializers.CharField(source='cost_center.name', read_only=True)
    
    class Meta:
        model = GLJournalLine
        fields = [
            'id', 'line_number', 'account', 'account_code', 'account_name',
            'cost_center', 'cost_center_name', 'debit_amount', 'credit_amount',
            'description', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

class GLJournalSerializer(serializers.ModelSerializer):
    """GL Journal with lines"""
    lines = GLJournalLineSerializer(many=True, required=False)
    line_count = serializers.SerializerMethodField()
    is_balanced = serializers.SerializerMethodField()
    
    class Meta:
        model = GLJournal
        fields = [
            'id', 'journal_number', 'posting_date', 'reference', 'narration',
            'total_debit', 'total_credit', 'status', 'lines', 'line_count',
            'is_balanced', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'journal_number', 'is_balanced', 'created_at', 'updated_at']
    
    def get_line_count(self, obj):
        """Count journal lines"""
        return obj.lines.count()
    
    def get_is_balanced(self, obj):
        """Check if journal is balanced"""
        return obj.total_debit == obj.total_credit

    
    def create(self, validated_data):
        lines_data = validated_data.pop('lines', [])
        journal = GLJournal.objects.create(**validated_data)
        tenant = journal.tenant
        created_by = journal.created_by

        for line_data in lines_data:
            GLJournalLine.objects.create(
                journal=journal,
                tenant=tenant,
                created_by=created_by,
                **line_data
            )
        return journal


# ===== SPECIALIZED SERIALIZERS =====

class BulkProductionEntrySerializer(serializers.Serializer):
    """Bulk production entry for shift handovers"""
    entries = ProductionEntrySerializer(many=True)
    shift = serializers.CharField(max_length=20)
    entry_date = serializers.DateField()
    
    def validate_entries(self, value):
        """Validate all entries in bulk"""
        if not value:
            raise serializers.ValidationError("At least one entry required")
        
        # Check for duplicate entries (same work_order + equipment + hour)
        seen_combinations = set()
        for entry_data in value:
            key = (
                entry_data.get('work_order').id if entry_data.get('work_order') else None,
                entry_data.get('equipment').id if entry_data.get('equipment') else None,
                entry_data.get('entry_datetime')
            )
            if key in seen_combinations:
                raise serializers.ValidationError("Duplicate entries detected")
            seen_combinations.add(key)
        
        return value

class StockTransferSerializer(serializers.Serializer):
    """Stock transfer between warehouses"""
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.none())
    from_warehouse = serializers.PrimaryKeyRelatedField(queryset=Warehouse.objects.none())
    to_warehouse = serializers.PrimaryKeyRelatedField(queryset=Warehouse.objects.none())
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0.001)
    reason = serializers.CharField(max_length=200, required=False)
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'context' in kwargs and 'request' in kwargs['context']:
            # Set queryset based on tenant
            from .middleware import get_current_tenant
            tenant = get_current_tenant()
            if tenant:
                self.fields['product'].queryset = Product.objects.filter(tenant=tenant, is_active=True)
                self.fields['from_warehouse'].queryset = Warehouse.objects.filter(tenant=tenant, is_active=True)
                self.fields['to_warehouse'].queryset = Warehouse.objects.filter(tenant=tenant, is_active=True)
    
    def validate(self, data):
        """Validate transfer request"""
        if data['from_warehouse'] == data['to_warehouse']:
            raise serializers.ValidationError("Source and destination warehouses cannot be the same")
        
        # Check available stock
        from django.db.models import Sum
        from .middleware import get_current_tenant
        
        tenant = get_current_tenant()
        available_stock = StockMovement.objects.filter(
            tenant=tenant,
            product=data['product'],
            warehouse=data['from_warehouse']
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        if available_stock < data['quantity']:
            raise serializers.ValidationError(
                f"Insufficient stock in {data['from_warehouse'].warehouse_name}. "
                f"Available: {available_stock}, Required: {data['quantity']}"
            )
        
        return data

class CSVImportSerializer(serializers.Serializer):
    """CSV import with field mapping"""
    data_type = serializers.ChoiceField(choices=['products', 'employees', 'stock_movements', 'parties'])
    csv_file = serializers.FileField()
    field_mapping = serializers.JSONField()
    skip_header = serializers.BooleanField(default=True)
    validate_only = serializers.BooleanField(default=False)
    
    def validate_csv_file(self, value):
        """Validate CSV file"""
        if not value.name.endswith('.csv'):
            raise serializers.ValidationError("Only CSV files are allowed")
        
        if value.size > 5 * 1024 * 1024:  # 5MB limit
            raise serializers.ValidationError("File size cannot exceed 5MB")
        
        return value
    
    def validate_field_mapping(self, value):
        """Validate field mapping configuration"""
        if not isinstance(value, dict):
            raise serializers.ValidationError("Field mapping must be a dictionary")
        
        if not value:
            raise serializers.ValidationError("At least one field mapping required")
        
        return value

# ===== DASHBOARD SERIALIZERS =====

class DashboardSummarySerializer(serializers.Serializer):
    """Executive dashboard summary"""
    production_summary = serializers.DictField()
    inventory_alerts = serializers.ListField()
    financial_summary = serializers.DictField()
    recent_activities = serializers.ListField()
    kpi_scores = serializers.DictField()

class OEEMetricsSerializer(serializers.Serializer):
    """OEE metrics response"""
    equipment_id = serializers.IntegerField()
    equipment_name = serializers.CharField()
    date = serializers.DateField()
    oee = serializers.DecimalField(max_digits=5, decimal_places=2)
    availability = serializers.DecimalField(max_digits=5, decimal_places=2)
    performance = serializers.DecimalField(max_digits=5, decimal_places=2)
    quality = serializers.DecimalField(max_digits=5, decimal_places=2)
    total_hours = serializers.IntegerField()
    downtime_hours = serializers.DecimalField(max_digits=5, decimal_places=2)

class ProductivityReportSerializer(serializers.Serializer):
    """Employee productivity report"""
    employee_id = serializers.IntegerField()
    employee_code = serializers.CharField()
    full_name = serializers.CharField()
    department = serializers.CharField()
    total_produced = serializers.IntegerField()
    total_rejected = serializers.IntegerField()
    quality_rate = serializers.DecimalField(max_digits=5, decimal_places=2)
    avg_hourly_output = serializers.DecimalField(max_digits=8, decimal_places=2)
    hours_worked = serializers.IntegerField()
    efficiency_rating = serializers.SerializerMethodField()
    
    def get_efficiency_rating(self, obj):
        """Get efficiency rating based on performance"""
        if obj['quality_rate'] >= 95 and obj['avg_hourly_output'] >= 100:
            return 'EXCELLENT'
        elif obj['quality_rate'] >= 90 and obj['avg_hourly_output'] >= 80:
            return 'GOOD'
        elif obj['quality_rate'] >= 85 and obj['avg_hourly_output'] >= 60:
            return 'AVERAGE'
        else:
            return 'NEEDS_IMPROVEMENT'

# ===== VALIDATION HELPERS =====

class TenantValidationMixin:
    """Mixin to add tenant validation to serializers"""
    
    def validate_tenant_object(self, obj, field_name):
        """Ensure object belongs to current tenant"""
        from .middleware import get_current_tenant
        tenant = get_current_tenant()
        
        if tenant and obj.tenant != tenant:
            raise serializers.ValidationError(f"{field_name} does not belong to current tenant")
        
        return obj

# ===== BULK OPERATION SERIALIZERS =====

class BulkUpdateSerializer(serializers.Serializer):
    """Generic bulk update operations"""
    operation = serializers.ChoiceField(choices=['update', 'delete', 'activate', 'deactivate'])
    object_ids = serializers.ListField(child=serializers.IntegerField())
    update_data = serializers.DictField(required=False)
    
    def validate_object_ids(self, value):
        """Validate object IDs"""
        if not value:
            raise serializers.ValidationError("At least one object ID required")
        if len(value) > 100:
            raise serializers.ValidationError("Cannot process more than 100 objects at once")
        return value

class WorkOrderBulkUpdateSerializer(BulkUpdateSerializer):
    """Bulk work order operations"""
    allowed_operations = ['update_status', 'update_priority', 'assign_cost_center']
    
    def validate(self, data):
        """Validate work order bulk operations"""
        operation = data['operation']
        
        if operation == 'update_status':
            if 'status' not in data.get('update_data', {}):
                raise serializers.ValidationError("Status required for status update operation")
        
        elif operation == 'assign_cost_center':
            if 'cost_center_id' not in data.get('update_data', {}):
                raise serializers.ValidationError("Cost center ID required for assignment operation")
        
        return data

# ===== REPORTING SERIALIZERS =====

class StockReportSerializer(serializers.Serializer):
    """Stock report export"""
    sku = serializers.CharField()
    product_name = serializers.CharField()
    current_stock = serializers.DecimalField(max_digits=12, decimal_places=3)
    reorder_point = serializers.IntegerField()
    standard_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    stock_value = serializers.DecimalField(max_digits=15, decimal_places=2)
    last_movement_date = serializers.DateTimeField(allow_null=True)
    warehouse_breakdown = serializers.ListField()

class ProductionReportSerializer(serializers.Serializer):
    """Production summary report"""
    date = serializers.DateField()
    work_order = serializers.CharField()
    product_sku = serializers.CharField()
    equipment = serializers.CharField()
    operator = serializers.CharField()
    shift = serializers.CharField()
    quantity_produced = serializers.IntegerField()
    quantity_rejected = serializers.IntegerField()
    downtime_minutes = serializers.IntegerField()
    efficiency_rate = serializers.DecimalField(max_digits=5, decimal_places=2)

class FinancialSummarySerializer(serializers.Serializer):
    """Financial summary for management"""
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    revenue = serializers.DecimalField(max_digits=15, decimal_places=2)
    cogs = serializers.DecimalField(max_digits=15, decimal_places=2)
    expenses = serializers.DecimalField(max_digits=15, decimal_places=2)
    gross_profit = serializers.DecimalField(max_digits=15, decimal_places=2)
    net_profit = serializers.DecimalField(max_digits=15, decimal_places=2)
    gross_margin_pct = serializers.DecimalField(max_digits=5, decimal_places=2)

# ===== API RESPONSE SERIALIZERS =====

class APIResponseSerializer(serializers.Serializer):
    """Standard API response format"""
    success = serializers.BooleanField()
    message = serializers.CharField()
    data = serializers.JSONField(required=False)
    errors = serializers.ListField(required=False)
    pagination = serializers.DictField(required=False)

class AlertSerializer(serializers.Serializer):
    """Business alert serializer"""
    type = serializers.CharField()
    severity = serializers.ChoiceField(choices=['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
    message = serializers.CharField()
    action_required = serializers.CharField()
    reference = serializers.CharField()
    timestamp = serializers.DateTimeField()
    acknowledged = serializers.BooleanField(default=False)

class PurchaseOrderLineSerializer(serializers.ModelSerializer):
    product = serializers.CharField()  # Change to CharField to accept SKU instead of ID

    class Meta:
        model = PurchaseOrderLine
        fields = ['line_number', 'product', 'quantity', 'unit_price', 'subtotal']
        read_only_fields = ['subtotal']

    def validate_product(self, value):
        # Resolve SKU to Product instance
        tenant = self.context['request'].tenant
        try:
            return Product.objects.get(tenant=tenant, sku=value)
        except Product.DoesNotExist:
            raise serializers.ValidationError("Product with this SKU does not exist.")

    def validate(self, data):
        if not data.get('product'):
            raise serializers.ValidationError({"product": "This field is required."})
        return data

class PurchaseOrderSerializer(serializers.ModelSerializer):
    lines = PurchaseOrderLineSerializer(many=True, required=False)
    supplier = serializers.CharField()  # Optional: Allow supplier code instead of ID

    class Meta:
        model = PurchaseOrder
        fields = '__all__'
        read_only_fields = ['tenant', 'created_by', 'po_number', 'amount']

    def validate_supplier(self, value):
        # Resolve supplier code to Party instance
        tenant = self.context['request'].tenant
        try:
            return Party.objects.get(tenant=tenant, party_code=value, party_type='supplier')
        except Party.DoesNotExist:
            raise serializers.ValidationError("Supplier with this code does not exist.")

    def create(self, validated_data):
        lines_data = validated_data.pop('lines', [])
        tenant = self.context['request'].tenant
        user = self.context['request'].user
        
        # Auto-generate PO number
        last_po = PurchaseOrder.objects.filter(tenant=tenant).order_by('-id').first()
        po_number = f"PO-{timezone.now().strftime('%Y%m')}-{(last_po.id + 1) if last_po else 1:04d}"
        validated_data['po_number'] = po_number
        
        # Create the PurchaseOrder
        po = PurchaseOrder.objects.create(tenant=tenant, created_by=user, **validated_data)
        
        # Create lines and calculate amount
        amount = Decimal('0.00')
        for line_data in lines_data:
            line_data.pop('purchase_order', None)
            line = PurchaseOrderLine.objects.create(
                tenant=tenant,
                created_by=user,
                purchase_order=po,
                **line_data
            )
            amount += line.subtotal
        
        po.amount = amount
        po.save()
        
        return po
    
    def update(self, instance, validated_data):
        lines_data = validated_data.pop('lines', None)
        instance = super().update(instance, validated_data)
        
        if lines_data is not None:
            instance.lines.all().delete()
            amount = Decimal('0.00')
            
            for line_data in lines_data:
                line_data.pop('purchase_order', None)
                line = PurchaseOrderLine.objects.create(
                    tenant=instance.tenant,
                    created_by=self.context['request'].user,
                    purchase_order=instance,
                    **line_data
                )
                amount += line.subtotal
            
            instance.amount = amount
            instance.save()
        
        return instance

class CustomerInvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.display_name', read_only=True)
    customer_display_name = serializers.CharField(source='customer.display_name', read_only=True)
    document_url = serializers.SerializerMethodField(read_only=True)
    reference_customer_po_number = serializers.CharField(source='reference_customer_po.po_number', read_only=True)
    
    # Add amount as a writeable field that maps to invoice_amount
    amount = serializers.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        write_only=True, 
        required=False
    )

    class Meta:
        model = CustomerInvoice
        fields = [
            'id', 'invoice_number', 'customer', 'customer_name', 'customer_display_name',
            'reference_customer_po', 'reference_customer_po_number', 'invoice_date',
            'due_date', 'invoice_amount', 'amount', 'status', 'notes', 'invoice_document',
            'document_url', 'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_by', 'invoice_amount']

    def get_document_url(self, obj):
        return obj.invoice_document.url if obj and obj.invoice_document else None

    def validate_invoice_number(self, value):
        tenant = get_current_tenant()
        if not tenant:
            raise serializers.ValidationError("No tenant context")
        qs = CustomerInvoice.objects.filter(tenant=tenant, invoice_number=value)
        if self.instance:
            qs = qs.exclude(id=self.instance.id)
        if qs.exists():
            raise serializers.ValidationError(f"Invoice number {value} already exists")
        return value

    def validate(self, attrs):
        # Map 'amount' to 'invoice_amount' if provided
        if 'amount' in attrs:
            attrs['invoice_amount'] = attrs.pop('amount')
        
        # Auto-calculate due date if not provided
        if not attrs.get('due_date') and attrs.get('invoice_date'):
            customer = attrs.get('customer')
            if customer:
                payment_terms_days = getattr(customer, 'payment_terms', 30)
                from datetime import timedelta
                attrs['due_date'] = attrs['invoice_date'] + timedelta(days=payment_terms_days)
        
        return attrs

    def create(self, validated_data):
        tenant = get_current_tenant()
        if not tenant:
            raise serializers.ValidationError("No tenant context")

        request = self.context.get('request')
        if request and request.user:
            validated_data['created_by'] = request.user
        validated_data['tenant'] = tenant

        return super().create(validated_data)

    def update(self, instance, validated_data):
        # Handle document replacement
        new_doc = validated_data.get('invoice_document', None)
        if new_doc and instance.invoice_document:
            try:
                instance.invoice_document.delete(save=False)
            except Exception:
                pass

        return super().update(instance, validated_data)
        
class CustomerPurchaseOrderSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.display_name', read_only=True)
    customer_code = serializers.CharField(source='customer.party_code', read_only=True)
    document_url = serializers.SerializerMethodField()
    
    # Add amount field that maps to po_amount
    amount = serializers.DecimalField(
        max_digits=12, 
        decimal_places=2, 
        write_only=True, 
        required=False
    )
    
    class Meta:
        model = CustomerPurchaseOrder
        fields = [
            'id', 'po_number', 'customer', 'customer_name', 'customer_code', 
            'po_date', 'delivery_required_by', 'po_amount', 'amount', 'status', 
            'description', 'special_instructions', 'po_document', 'document_url',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['tenant', 'created_by', 'po_amount']
    
    def get_document_url(self, obj):
        """Get document URL if exists"""
        if obj.po_document and hasattr(obj.po_document, 'url'):
            return obj.po_document.url
        return None
    
    def validate_po_number(self, value):
        """Validate PO number uniqueness within tenant"""
        tenant = get_current_tenant()
        if not tenant:
            raise serializers.ValidationError("No tenant context")
        
        # Check if PO number already exists for this tenant and customer
        customer = self.initial_data.get('customer')
        if customer:
            qs = CustomerPurchaseOrder.objects.filter(
                tenant=tenant, 
                customer_id=customer,
                po_number=value
            )
            if self.instance:
                qs = qs.exclude(id=self.instance.id)
            if qs.exists():
                raise serializers.ValidationError(f"PO number {value} already exists for this customer")
        return value
    
    def validate(self, attrs):
        """Map amount field to po_amount and handle validation"""
        # Map 'amount' to 'po_amount' if provided
        if 'amount' in attrs:
            attrs['po_amount'] = attrs.pop('amount')
        
        return attrs
    
    def create(self, validated_data):
        """Create CustomerPurchaseOrder with proper tenant and user context"""
        tenant = get_current_tenant()
        if not tenant:
            raise serializers.ValidationError("No tenant context")
        
        request = self.context.get('request')
        if request and request.user:
            validated_data['created_by'] = request.user
        validated_data['tenant'] = tenant
        
        # Ensure is_active is True
        validated_data['is_active'] = True
        
        return super().create(validated_data)
    
    def update(self, instance, validated_data):
        """Handle document replacement safely"""
        new_doc = validated_data.get('po_document', None)
        if new_doc and instance.po_document:
            # Delete old document
            try:
                instance.po_document.delete(save=False)
            except Exception:
                logger.warning(f"Failed to delete old document for CPO {instance.id}")
        
        return super().update(instance, validated_data)

class PaymentAdviceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.display_name', read_only=True)
    mentioned_invoices_details = CustomerInvoiceSerializer(source='mentioned_invoices', many=True, read_only=True)
    
    class Meta:
        model = PaymentAdvice
        fields = '__all__'
        read_only_fields = ['tenant', 'created_by']