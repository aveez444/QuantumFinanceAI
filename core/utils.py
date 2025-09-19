# core/utils.py - ERP Utility Functions

from django.db.models import Sum, Avg, Q
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
from .models import GLJournal, GLJournalLine, ChartOfAccounts, WorkOrder
import logging
from .llm_utils import call_llm

logger = logging.getLogger(__name__)

def generate_movement_number(tenant, movement_type):
    """Generate unique movement numbers"""
    from .models import StockMovement
    
    prefix_map = {
        'receipt': 'REC',
        'issue': 'ISS',
        'transfer_out': 'TRF',
        'transfer_in': 'TRF',
        'adjustment': 'ADJ',
        'production_receipt': 'PROD',
        'production_issue': 'PROD'
    }
    
    prefix = prefix_map.get(movement_type, 'MOV')
    date_part = timezone.now().strftime('%Y%m')
    
    # Get last movement number for this tenant and type
    last_movement = StockMovement.objects.filter(
        tenant=tenant,
        movement_number__startswith=f"{prefix}-{date_part}"
    ).order_by('-id').first()
    
    if last_movement:
        try:
            last_seq = int(last_movement.movement_number.split('-')[-1])
            next_seq = last_seq + 1
        except (ValueError, IndexError):
            next_seq = 1
    else:
        next_seq = 1
    
    return f"{prefix}-{date_part}-{next_seq:04d}"
    
def calculate_oee(equipment, date_filter):
    """Calculate Overall Equipment Effectiveness (OEE) for a specific date"""
    from .models import ProductionEntry
    
    # Get production entries for the equipment on specified date
    entries = ProductionEntry.objects.filter(
        equipment=equipment,
        entry_datetime__date=date_filter
    )
    
    if not entries.exists():
        return {
            'oee': 0,
            'availability': 0,
            'performance': 0,
            'quality': 0,
            'total_hours': 0,
            'downtime_hours': 0,
            'total_produced': 0,
            'total_rejected': 0
        }
    
    # Calculate metrics
    total_entries = entries.count()
    total_downtime_minutes = entries.aggregate(Sum('downtime_minutes'))['downtime_minutes__sum'] or 0
    total_produced = entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
    total_rejected = entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    
    # Availability = (Total Time - Downtime) / Total Time
    total_minutes = total_entries * 60  # Assuming 1-hour entries
    availability = ((total_minutes - total_downtime_minutes) / max(total_minutes, 1)) * 100
    
    # Performance = Actual Output / Theoretical Capacity
    theoretical_capacity = equipment.capacity_per_hour * total_entries
    performance = (total_produced / max(theoretical_capacity, 1)) * 100 if theoretical_capacity > 0 else 0
    
    # Quality = Good Parts / Total Parts
    total_parts = total_produced + total_rejected
    quality = (total_produced / max(total_parts, 1)) * 100 if total_parts > 0 else 0
    
    # OEE = Availability × Performance × Quality / 10000
    oee = (availability * performance * quality) / 10000
    
    return {
        'oee': round(oee, 2),
        'availability': round(availability, 2),
        'performance': round(performance, 2),
        'quality': round(quality, 2),
        'total_hours': total_entries,
        'downtime_hours': round(total_downtime_minutes / 60, 2),
        'total_produced': total_produced,
        'total_rejected': total_rejected
    }
    
def calculate_inventory_valuation(tenant, valuation_date=None):
    from .models import Product, StockMovement
    from decimal import Decimal
    if not valuation_date:
        valuation_date = timezone.now().date()
    
    valuation_data = {}
    products = Product.objects.filter(tenant=tenant, is_active=True)
    
    for product in products:
        movements = StockMovement.objects.filter(
            tenant=tenant,
            product=product,
            movement_date__date__lte=valuation_date
        ).order_by('movement_date')
        
        running_qty = Decimal('0')
        running_value = Decimal('0')
        
        for movement in movements:
            if movement.movement_type in ['receipt', 'production_receipt', 'transfer_in', 'adjustment']:  # Inflows
                new_qty = running_qty + movement.quantity
                new_value = running_value + (movement.quantity * movement.unit_cost)
                if new_qty > 0:
                    unit_cost = new_value / new_qty
                running_qty = new_qty
                running_value = new_value
            else:  # Outflows
                if running_qty >= movement.quantity:
                    outflow_value = movement.quantity * (running_value / running_qty if running_qty > 0 else 0)
                    running_qty -= movement.quantity
                    running_value -= outflow_value
        
        valuation_data[product.sku] = {
            'product_name': product.product_name,
            'category': product.category or 'Uncategorized',
            'current_qty': float(running_qty),
            'average_cost': float(running_value / running_qty if running_qty > 0 else 0),
            'total_value': float(running_value),
            'reorder_point': product.reorder_point
        }
    
    total_value = sum(data['total_value'] for data in valuation_data.values())
    return {'total_inventory_value': total_value, 'details': valuation_data}

def calculate_material_consumption(work_order):
    """Calculate actual material consumption vs standard"""
    from .models import StockMovement
    
    # Get all production issues for this work order
    material_issues = StockMovement.objects.filter(
        tenant=work_order.tenant,
        movement_type='production_issue',
        reference_doc=work_order.wo_number
    )
    
    consumption_data = {}
    for issue in material_issues:
        sku = issue.product.sku
        if sku not in consumption_data:
            consumption_data[sku] = {
                'product_name': issue.product.product_name,
                'actual_consumed': 0,
                'total_cost': 0
            }
        
        consumption_data[sku]['actual_consumed'] += float(abs(issue.quantity))
        consumption_data[sku]['total_cost'] += float(abs(issue.quantity) * issue.unit_cost)
    
    return consumption_data

def get_production_efficiency_trends(tenant, days=7):
    """Get production efficiency trends over time"""
    from .models import ProductionEntry, Equipment
    
    end_date = timezone.now().date()
    start_date = end_date - timedelta(days=days)
    
    daily_trends = []
    
    for i in range(days):
        current_date = start_date + timedelta(days=i)
        
        day_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            entry_datetime__date=current_date
        )
        
        daily_production = day_entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
        daily_rejections = day_entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
        daily_downtime = day_entries.aggregate(Sum('downtime_minutes'))['downtime_minutes__sum'] or 0
        
        # Calculate daily efficiency
        total_output = daily_production + daily_rejections
        quality_rate = (daily_production / max(total_output, 1)) * 100
        
        daily_trends.append({
            'date': current_date.isoformat(),
            'production': daily_production,
            'rejections': daily_rejections,
            'quality_rate': round(quality_rate, 2),
            'downtime_hours': round(daily_downtime / 60, 2),
            'entries_count': day_entries.count()
        })
    
    return daily_trends

def validate_stock_transaction(tenant, product, warehouse, quantity, movement_type):
    """Validate stock transaction before processing"""
    from .models import StockMovement
    
    errors = []
    
    # Check if it's an issue/outgoing movement
    if movement_type in ['issue', 'transfer_out', 'production_issue'] and quantity > 0:
        # Check available stock
        current_stock = StockMovement.objects.filter(
            tenant=tenant,
            product=product,
            warehouse=warehouse
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        if current_stock < quantity:
            errors.append(f"Insufficient stock. Available: {current_stock}, Required: {quantity}")
    
    # Validate product is active
    if not product.is_active:
        errors.append("Product is not active")
    
    # Validate warehouse is active
    if not warehouse.is_active:
        errors.append("Warehouse is not active")
    
    return errors

def generate_reorder_suggestions(tenant):
    """Generate purchase suggestions based on reorder points"""
    from .models import Product, StockMovement, Party
    
    suggestions = []
    products = Product.objects.filter(tenant=tenant, is_active=True)
    
    for product in products:
        current_stock = StockMovement.objects.filter(
            tenant=tenant,
            product=product
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        if current_stock <= product.reorder_point:
            # Calculate suggested order quantity (simple economic order quantity)
            shortage = product.reorder_point - current_stock
            suggested_qty = max(shortage, product.reorder_point * 2)  # Order for 2x reorder point
            
            # Find preferred supplier (would come from purchase history)
            # For now, just get any supplier
            preferred_supplier = Party.objects.filter(
                tenant=tenant,
                party_type='supplier',
                is_active=True
            ).first()
            
            suggestions.append({
                'product_sku': product.sku,
                'product_name': product.product_name,
                'current_stock': float(current_stock),
                'reorder_point': product.reorder_point,
                'shortage': float(shortage),
                'suggested_order_qty': float(suggested_qty),
                'estimated_cost': float(suggested_qty * product.standard_cost),
                'preferred_supplier': preferred_supplier.display_name if preferred_supplier else 'Not Set',
                'urgency': 'HIGH' if current_stock <= 0 else 'MEDIUM'
            })
    
    # Sort by urgency and shortage amount
    suggestions.sort(key=lambda x: (x['urgency'] == 'HIGH', abs(x['shortage'])), reverse=True)
    
    return suggestions

def calculate_labor_efficiency(employee, date_filter):
    """Calculate individual employee efficiency"""
    from .models import ProductionEntry, WorkOrder
    
    # Get production entries for this employee
    entries = ProductionEntry.objects.filter(
        operator=employee,
        entry_datetime__date=date_filter
    ).select_related('work_order')
    
    if not entries.exists():
        return {
            'efficiency_rate': 0,
            'quality_rate': 0,
            'hours_worked': 0,
            'total_produced': 0
        }
    
    total_produced = entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
    total_rejected = entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    hours_worked = entries.count()  # 1 entry per hour
    
    # Calculate expected production based on work orders
    expected_production = 0
    for entry in entries:
        # Get the target rate from equipment capacity
        if entry.equipment.capacity_per_hour > 0:
            expected_production += entry.equipment.capacity_per_hour
    
    efficiency_rate = (total_produced / max(expected_production, 1)) * 100 if expected_production > 0 else 0
    quality_rate = (total_produced / max(total_produced + total_rejected, 1)) * 100
    
    return {
        'efficiency_rate': round(efficiency_rate, 2),
        'quality_rate': round(quality_rate, 2),
        'hours_worked': hours_worked,
        'total_produced': total_produced,
        'total_rejected': total_rejected,
        'expected_production': expected_production
    }

def generate_production_schedule_suggestions(tenant, days_ahead=7):
    """Suggest production schedule based on demand and capacity"""
    from .models import WorkOrder, Equipment, ProductionEntry
    
    # Get pending work orders
    pending_orders = WorkOrder.objects.filter(
        tenant=tenant,
        status__in=['planned', 'released'],
        is_active=True
    ).order_by('due_date', 'priority')
    
    # Get equipment capacity
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    
    schedule_suggestions = []
    current_date = timezone.now().date()
    
    for equipment in equipment_list:
        # Calculate current utilization
        recent_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__gte=current_date - timedelta(days=7)
        ).count()
        
        weekly_utilization = (recent_entries / (7 * 24)) * 100  # Assuming 24-hour operation
        
        # Find suitable work orders for this equipment
        suitable_orders = []
        for wo in pending_orders:
            # Simple logic: match if equipment has sufficient capacity
            estimated_hours = wo.quantity_planned / max(equipment.capacity_per_hour, 1)
            
            suitable_orders.append({
                'wo_number': wo.wo_number,
                'product_sku': wo.product.sku,
                'quantity_planned': wo.quantity_planned,
                'due_date': wo.due_date.isoformat(),
                'estimated_hours': round(estimated_hours, 2),
                'priority': wo.priority
            })
        
        schedule_suggestions.append({
            'equipment_code': equipment.equipment_code,
            'equipment_name': equipment.equipment_name,
            'current_utilization': round(weekly_utilization, 2),
            'capacity_per_hour': equipment.capacity_per_hour,
            'suggested_orders': suitable_orders[:5]  # Top 5 suggestions
        })
    
    return schedule_suggestions

def calculate_cost_center_performance(cost_center, period_start, period_end):
    """Calculate cost center performance metrics"""
    from .models import GLJournalLine, Employee, ProductionEntry
    
    # Get GL entries for this cost center
    gl_entries = GLJournalLine.objects.filter(
        cost_center=cost_center,
        journal__status='posted',
        journal__posting_date__range=[period_start, period_end]
    )
    
    # Calculate costs (expenses and COGS)
    total_costs = 0
    for entry in gl_entries:
        if entry.account.account_type in ['expense', 'cogs']:
            total_costs += float(entry.debit_amount - entry.credit_amount)
    
    # Get employees in this cost center
    employees = Employee.objects.filter(
        cost_center=cost_center,
        is_active=True
    )
    
    # Calculate production output from this cost center
    total_production = 0
    for employee in employees:
        production = ProductionEntry.objects.filter(
            operator=employee,
            entry_datetime__date__range=[period_start, period_end]
        ).aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
        total_production += production
    
    # Calculate metrics
    cost_per_unit = total_costs / max(total_production, 1) if total_production > 0 else 0
    
    return {
        'cost_center_code': cost_center.cost_center_code,
        'cost_center_name': cost_center.name,
        'period': {'start': period_start, 'end': period_end},
        'total_costs': total_costs,
        'total_production': total_production,
        'cost_per_unit': round(cost_per_unit, 4),
        'employee_count': employees.count(),
        'avg_cost_per_employee': total_costs / max(employees.count(), 1)
    }

# Add this updated function to your utils.py file

def detect_production_anomalies(tenant, lookback_days=30):  # Changed default from 7 to 30 days
    """Detect unusual patterns in production data - improved version"""
    from .models import ProductionEntry, Equipment
    
    anomalies = []
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    
    end_date = timezone.now().date()
    start_date = end_date - timedelta(days=lookback_days)
    
    # Calculate baseline period (previous period of same length)
    baseline_start = start_date - timedelta(days=lookback_days)
    baseline_end = start_date - timedelta(days=1)
    
    for equipment in equipment_list:
        # Get baseline data for comparison
        baseline_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[baseline_start, baseline_end]
        )
        
        # Get recent production data
        entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[start_date, end_date]
        )
        
        if baseline_entries.count() < 3 or entries.count() == 0:  # Need minimum data points
            continue
        
        # Calculate baseline averages
        baseline_avg_production = baseline_entries.aggregate(Avg('quantity_produced'))['quantity_produced__avg'] or 0
        baseline_avg_rejection = baseline_entries.aggregate(Avg('quantity_rejected'))['quantity_rejected__avg'] or 0
        baseline_avg_downtime = baseline_entries.aggregate(Avg('downtime_minutes'))['downtime_minutes__avg'] or 0
        
        # Check each entry for anomalies
        for entry in entries:
            # Production significantly below baseline
            if entry.quantity_produced < baseline_avg_production * 0.7 and baseline_avg_production > 0:
                anomalies.append({
                    'type': 'low_production',
                    'equipment': equipment.equipment_name,
                    'equipment_code': equipment.equipment_code,
                    'timestamp': entry.entry_datetime,
                    'actual': entry.quantity_produced,
                    'expected': round(baseline_avg_production, 2),
                    'severity': 'HIGH' if entry.quantity_produced < baseline_avg_production * 0.5 else 'MEDIUM',
                    'deviation_pct': round(((entry.quantity_produced - baseline_avg_production) / baseline_avg_production) * 100, 2),
                    'operator': entry.operator.full_name if entry.operator else 'Unknown'
                })
            
            # Rejection rate significantly above baseline
            if entry.quantity_rejected > baseline_avg_rejection * 2 and entry.quantity_rejected > 0:
                anomalies.append({
                    'type': 'high_rejection',
                    'equipment': equipment.equipment_name,
                    'equipment_code': equipment.equipment_code,
                    'timestamp': entry.entry_datetime,
                    'actual': entry.quantity_rejected,
                    'expected': round(baseline_avg_rejection, 2),
                    'severity': 'HIGH',
                    'operator': entry.operator.full_name if entry.operator else 'Unknown'
                })
            
            # Excessive downtime
            if entry.downtime_minutes > baseline_avg_downtime * 2 and entry.downtime_minutes > 30:
                anomalies.append({
                    'type': 'excessive_downtime',
                    'equipment': equipment.equipment_name,
                    'equipment_code': equipment.equipment_code,
                    'timestamp': entry.entry_datetime,
                    'downtime_minutes': entry.downtime_minutes,
                    'expected': round(baseline_avg_downtime, 2),
                    'reason': entry.downtime_reason or 'Not specified',
                    'severity': 'HIGH' if entry.downtime_minutes > baseline_avg_downtime * 3 else 'MEDIUM',
                    'operator': entry.operator.full_name if entry.operator else 'Unknown'
                })
    
    # Sort by severity and timestamp
    anomalies.sort(key=lambda x: (x['severity'] == 'HIGH', x['timestamp']), reverse=True)
    
    return anomalies

def detect_production_anomalies_with_range(tenant, start_date, end_date):
    """Detect unusual patterns in production data within specific date range"""
    from .models import ProductionEntry, Equipment
    
    anomalies = []
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    
    # Calculate baseline period (previous period of same length)
    period_length = (end_date - start_date).days
    baseline_start = start_date - timedelta(days=period_length)
    baseline_end = start_date - timedelta(days=1)
    
    for equipment in equipment_list:
        # Get baseline data
        baseline_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[baseline_start, baseline_end]
        )
        
        # Get current period data
        current_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[start_date, end_date]
        )
        
        if baseline_entries.count() < 3:  # Need minimum baseline data
            continue
        
        # Calculate baseline averages
        baseline_avg_production = baseline_entries.aggregate(Avg('quantity_produced'))['quantity_produced__avg'] or 0
        baseline_avg_rejection = baseline_entries.aggregate(Avg('quantity_rejected'))['quantity_rejected__avg'] or 0
        baseline_avg_downtime = baseline_entries.aggregate(Avg('downtime_minutes'))['downtime_minutes__avg'] or 0
        
        # Check current period entries for anomalies
        for entry in current_entries:
            # Production significantly below baseline
            if entry.quantity_produced < baseline_avg_production * 0.7 and baseline_avg_production > 0:
                anomalies.append({
                    'type': 'low_production',
                    'equipment': equipment.equipment_name,
                    'equipment_code': equipment.equipment_code,
                    'timestamp': entry.entry_datetime.isoformat(),
                    'date': entry.entry_datetime.date().isoformat(),
                    'actual': entry.quantity_produced,
                    'baseline_avg': round(baseline_avg_production, 2),
                    'deviation_pct': round(((entry.quantity_produced - baseline_avg_production) / baseline_avg_production) * 100, 2),
                    'severity': 'HIGH' if entry.quantity_produced < baseline_avg_production * 0.5 else 'MEDIUM',
                    'operator': entry.operator.full_name if entry.operator else 'Unknown'
                })
            
            # Rejection rate significantly above baseline
            if entry.quantity_rejected > baseline_avg_rejection * 2 and entry.quantity_rejected > 0:
                anomalies.append({
                    'type': 'high_rejection',
                    'equipment': equipment.equipment_name,
                    'equipment_code': equipment.equipment_code,
                    'timestamp': entry.entry_datetime.isoformat(),
                    'date': entry.entry_datetime.date().isoformat(),
                    'actual': entry.quantity_rejected,
                    'baseline_avg': round(baseline_avg_rejection, 2),
                    'deviation_pct': round(((entry.quantity_rejected - baseline_avg_rejection) / max(baseline_avg_rejection, 1)) * 100, 2),
                    'severity': 'HIGH',
                    'operator': entry.operator.full_name if entry.operator else 'Unknown'
                })
            
            # Excessive downtime compared to baseline
            if entry.downtime_minutes > baseline_avg_downtime * 2 and entry.downtime_minutes > 30:
                anomalies.append({
                    'type': 'excessive_downtime',
                    'equipment': equipment.equipment_name,
                    'equipment_code': equipment.equipment_code,
                    'timestamp': entry.entry_datetime.isoformat(),
                    'date': entry.entry_datetime.date().isoformat(),
                    'downtime_minutes': entry.downtime_minutes,
                    'baseline_avg': round(baseline_avg_downtime, 2),
                    'reason': entry.downtime_reason or 'Not specified',
                    'severity': 'HIGH' if entry.downtime_minutes > baseline_avg_downtime * 3 else 'MEDIUM',
                    'operator': entry.operator.full_name if entry.operator else 'Unknown'
                })
    
    # Sort by severity, then by timestamp (most recent first)
    anomalies.sort(key=lambda x: (
        0 if x['severity'] == 'HIGH' else 1,  # HIGH severity first
        -datetime.fromisoformat(x['timestamp'].replace('Z', '+00:00')).timestamp()  # Recent first
    ))
    
    return anomalies

def generate_financial_summary(tenant, period_start, period_end):
    """Generate financial summary for management reporting"""
    from .models import GLJournalLine, ChartOfAccounts
    
    # Get all posted journal lines for the period
    journal_lines = GLJournalLine.objects.filter(
        tenant=tenant,
        journal__status='posted',
        journal__posting_date__range=[period_start, period_end]
    ).select_related('account')
    
    # Aggregate by account type
    account_summary = {
        'revenue': 0,
        'cogs': 0,
        'expense': 0,
        'asset': 0,
        'liability': 0,
        'equity': 0
    }
    
    for line in journal_lines:
        account_type = line.account.account_type
        if account_type in account_summary:
            # For P&L accounts (revenue, cogs, expense): credit increases revenue, debit increases costs
            # For balance sheet: debit increases assets/expenses, credit increases liabilities/equity/revenue
            if account_type == 'revenue':
                account_summary[account_type] += float(line.credit_amount - line.debit_amount)
            else:
                account_summary[account_type] += float(line.debit_amount - line.credit_amount)
    
    # Calculate derived metrics
    gross_profit = account_summary['revenue'] - account_summary['cogs']
    net_profit = gross_profit - account_summary['expense']
    gross_margin = (gross_profit / max(account_summary['revenue'], 1)) * 100 if account_summary['revenue'] > 0 else 0
    
    return {
        'period': {'start': period_start, 'end': period_end},
        'revenue': account_summary['revenue'],
        'cogs': account_summary['cogs'],
        'expenses': account_summary['expense'],
        'gross_profit': gross_profit,
        'net_profit': net_profit,
        'gross_margin_pct': round(gross_margin, 2),
        'total_assets': account_summary['asset'],
        'total_liabilities': account_summary['liability']
    }


def create_automated_gl_entry(tenant, entry_type, reference_data, user=None):
    """Create automated GL entries based on business events"""
    from .models import GLJournal, GLJournalLine, ChartOfAccounts, WorkOrder
    from django.utils import timezone

    journal_numbers = []

    if entry_type == 'production_completion':
        wo_id = reference_data.get('work_order_id')
        work_order = WorkOrder.objects.get(id=wo_id, tenant=tenant)

        # Calculate production value (qty × standard cost)
        production_value = work_order.quantity_completed * work_order.product.standard_cost

        if production_value > 0:
            # Ensure accounts exist
            inventory_account, _ = ChartOfAccounts.objects.get_or_create(
                tenant=tenant,
                account_code='1300',
                defaults={
                    'account_name': 'Inventory',
                    'account_type': 'asset'
                }
            )
            cash_account, _ = ChartOfAccounts.objects.get_or_create(
                tenant=tenant,
                account_code='1000',
                defaults={
                    'account_name': 'Cash',
                    'account_type': 'asset'
                }
            )

            # Create journal header
            journal_number = generate_journal_number(tenant)
            journal = GLJournal.objects.create(
                tenant=tenant,
                journal_number=journal_number,
                posting_date=timezone.now().date(),
                reference=f"Production Completion - {work_order.wo_number}",
                narration=f"Completed {work_order.quantity_completed} units of {work_order.product.sku}",
                total_debit=production_value,
                total_credit=production_value,
                status='posted',  # Directly posted
                created_by=user
            )

            # Dr Inventory (Finished Goods)
            GLJournalLine.objects.create(
                tenant=tenant,
                journal=journal,
                line_number=1,
                account=inventory_account,
                cost_center=work_order.cost_center,
                debit_amount=production_value,
                description=f"Finished goods received: {work_order.product.sku}",
                created_by=user
            )

            # Cr Cash
            GLJournalLine.objects.create(
                tenant=tenant,
                journal=journal,
                line_number=2,
                account=cash_account,
                cost_center=work_order.cost_center,
                credit_amount=production_value,
                description=f"Cash spent for production: {work_order.product.sku}",
                created_by=user
            )

            journal_numbers.append(journal_number)

    return journal_numbers

def generate_journal_number(tenant):
    """Generate unique journal number"""
    from .models import GLJournal
    from django.utils import timezone
    
    count = GLJournal.objects.filter(tenant=tenant).count()
    return f"GL-{timezone.now().strftime('%Y%m')}-{(count + 1):04d}"

def get_dashboard_alerts(tenant):
    """Generate real-time business alerts"""
    alerts = []
    
    # Stock alerts
    reorder_suggestions = generate_reorder_suggestions(tenant)
    for suggestion in reorder_suggestions[:5]:  # Top 5 critical items
        alerts.append({
            'type': 'stock_low',
            'severity': suggestion['urgency'],
            'message': f"Low stock: {suggestion['product_sku']} ({suggestion['current_stock']} units)",
            'action_required': 'Create purchase order',
            'reference': suggestion['product_sku']
        })
    
    # Production alerts
    anomalies = detect_production_anomalies(tenant, lookback_days=1)
    for anomaly in anomalies[:3]:  # Top 3 production issues
        alerts.append({
            'type': 'production_anomaly',
            'severity': anomaly['severity'],
            'message': f"{anomaly['type'].replace('_', ' ').title()}: {anomaly['equipment']}",
            'action_required': 'Investigate equipment/process',
            'reference': anomaly['equipment']
        })
    
    # Work order alerts
    from .models import WorkOrder
    overdue_orders = WorkOrder.objects.filter(
        tenant=tenant,
        due_date__lt=timezone.now().date(),
        status__in=['planned', 'released', 'in_progress']
    ).count()
    
    if overdue_orders > 0:
        alerts.append({
            'type': 'overdue_orders',
            'severity': 'HIGH',
            'message': f"{overdue_orders} work orders are overdue",
            'action_required': 'Review production schedule',
            'reference': 'work_orders'
        })
    
    # Sort by severity
    severity_order = {'HIGH': 1, 'MEDIUM': 2, 'LOW': 3}
    alerts.sort(key=lambda x: severity_order.get(x['severity'], 4))
    
    return alerts

    # Placeholder for LLM API - replace with e.g., openai.ChatCompletion.create or xai equivalent
def call_llm(prompt):
    # TODO: Implement actual LLM call here
    # For testing, return a mock classification/response
    return "Mock LLM response: Classified as system-specific DB query."

# logger = logging.getLogger(__name__)

# def process_ai_query(tenant, user, query):
#     """
#     Core AI processing: Auto-detect intent, handle DB/API, generate response.
#     Optimizes for professional DB queries (efficient ORM for simple/complex cases).
#     """
#     # Step 1: Intent detection via LLM
#     intent_prompt = f"""
#     Analyze this user query: '{query}'.
#     Classify as:
#     - 'db_query': For direct data fetch (e.g., 'x product sales' -> suggest ORM like Product.objects.filter(...)).
#     - 'api_call': For analytics (e.g., 'production anomalies' -> call detect_production_anomalies).
#     - 'general': Pure knowledge (e.g., 'Taj Mahal location').
#     - 'hybrid': Mix (e.g., explain data with tips).
#     Suggest handling: For db_query, provide executable Django ORM code snippet.
#     For api_call, name the util/view to call.
#     Be precise, efficient, and insightful.
#     """
#     intent_response = call_llm(intent_prompt)
#     # Parse intent (in real LLM, extract from response; mock for now)
#     classification = 'db_query'  # Example parse

#     # Step 2: Handle based on classification
#     data = None
#     if classification == 'db_query':
#         # Generate and execute professional ORM query
#         # LLM suggests code like: "data = ProductionEntry.objects.filter(tenant=tenant, product__sku='X').aggregate(total_sales=Sum('quantity_produced'))"
#         orm_code = "data = Product.objects.filter(tenant=tenant, is_active=True).count()"  # LLM-generated example
#         try:
#             local_vars = {'tenant': tenant, 'user': user, **globals()}  # Safe env with models
#             exec(orm_code, local_vars)  # Execute in restricted scope (add safeguards like no __builtins__ overrides)
#             data = local_vars.get('data')
#         except Exception as e:
#             raise ValueError(f"DB query error: {e}")
    
#     elif classification == 'api_call':
#         # Call your utils/views, e.g., detect_production_anomalies(tenant)
#         data = detect_production_anomalies(tenant)  # Dynamic based on LLM suggestion
    
#     elif classification == 'hybrid' or classification == 'general':
#         # Direct LLM for explanation/tips
#         data = call_llm(f"Answer: '{query}'. Use knowledge for insights.")

#     # Step 3: Generate final response via LLM (format with tables, recommendations)
#     response_prompt = f"Based on data: {data}. Respond to '{query}' insightfully. Use tables for data, add recommendations."
#     final_response = call_llm(response_prompt)
#     return final_response