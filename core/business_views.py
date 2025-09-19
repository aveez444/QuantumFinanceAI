# core/business_views.py - Advanced Business Logic Views

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.db.models import Sum, Avg, Count, Q, F, Case, When
from django.utils import timezone
from datetime import datetime, timedelta
from decimal import Decimal
import logging
from django.shortcuts import get_object_or_404
from rest_framework import status

from .models import (
    WorkOrder, ProductionEntry, Equipment, Employee, Product,
    StockMovement, GLJournalLine, ChartOfAccounts, CostCenter
)
from .middleware import get_current_tenant
from .utils import (
    generate_reorder_suggestions, calculate_inventory_valuation,
    get_production_efficiency_trends, calculate_cost_center_performance, calculate_material_consumption, detect_production_anomalies,
    generate_financial_summary, create_automated_gl_entry, calculate_oee, 
    get_dashboard_alerts
)

logger = logging.getLogger(__name__)

# ===== HELPER FUNCTIONS =====

def parse_date_range(request, default_days=30):
    """Parse start_date and end_date from request or use default"""
    start_date_param = request.query_params.get('start_date')
    end_date_param = request.query_params.get('end_date')
    
    try:
        if end_date_param:
            end_date = datetime.strptime(end_date_param, '%Y-%m-%d').date()
        else:
            end_date = timezone.now().date()
            
        if start_date_param:
            start_date = datetime.strptime(start_date_param, '%Y-%m-%d').date()
        else:
            start_date = end_date - timedelta(days=default_days)
            
        # Ensure start_date is before end_date
        if start_date > end_date:
            start_date, end_date = end_date, start_date
            
        return start_date, end_date
    except ValueError:
        return None, None

def safe_aggregate(queryset, field, operation=Sum, default=0):
    """Safely aggregate field values with proper type conversion"""
    result = queryset.aggregate(value=operation(field))['value']
    if result is None:
        return default
    try:
        return float(result)
    except (TypeError, ValueError):
        return default

# ===== PRODUCTION PLANNING =====

@api_view(['GET'])
def production_schedule_suggestions(request):
    """AI-driven production schedule suggestions"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    days_ahead = int(request.query_params.get('days_ahead', 7))
    
    # Get pending work orders
    pending_orders = WorkOrder.objects.filter(
        tenant=tenant,
        status__in=['planned', 'released'],
        is_active=True
    ).select_related('product', 'cost_center').order_by('due_date', 'priority')
    
    # Get equipment capacity and utilization
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    equipment_schedule = []
    
    for equipment in equipment_list:
        # Calculate current week utilization with custom date range
        start_date, end_date = parse_date_range(request, 7)
        if start_date is None:
            return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
            
        recent_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[start_date, end_date]
        ).count()
        
        # Calculate total hours in the period
        total_hours = ((end_date - start_date).days + 1) * 24
        utilization_pct = (recent_entries / max(total_hours, 1)) * 100
        
        # Find suitable work orders for this equipment
        suitable_orders = []
        for wo in pending_orders:
            # Calculate estimated production time
            if equipment.capacity_per_hour > 0:
                estimated_hours = wo.quantity_planned / equipment.capacity_per_hour
                urgency_score = calculate_urgency_score(wo)
                
                suitable_orders.append({
                    'wo_number': wo.wo_number,
                    'product_sku': wo.product.sku,
                    'quantity_planned': wo.quantity_planned,
                    'due_date': wo.due_date.isoformat(),
                    'estimated_hours': round(estimated_hours, 2),
                    'urgency_score': urgency_score,
                    'priority': wo.priority
                })
        
        # Sort by urgency and due date
        suitable_orders.sort(key=lambda x: (x['urgency_score'], x['due_date']), reverse=True)
        
        equipment_schedule.append({
            'equipment_id': equipment.id,
            'equipment_code': equipment.equipment_code,
            'equipment_name': equipment.equipment_name,
            'capacity_per_hour': equipment.capacity_per_hour,
            'current_utilization_pct': round(utilization_pct, 2),
            'recommended_orders': suitable_orders[:5],
            'available_capacity_pct': max(0, 100 - utilization_pct),
            'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}
        })
    
    return Response({
        'schedule_date': timezone.now().date(),
        'planning_horizon_days': days_ahead,
        'equipment_schedule': equipment_schedule,
        'total_pending_orders': pending_orders.count(),
        'bottleneck_equipment': get_bottleneck_equipment(equipment_schedule)
    })

@api_view(['GET'])
def capacity_analysis(request):
    """Equipment capacity vs demand analysis"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    analysis_period = (end_date - start_date).days
    
    equipment_analysis = []
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    
    for equipment in equipment_list:
        # Historical utilization
        production_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[start_date, end_date]
        )
        
        total_hours_available = analysis_period * 24  # Assuming 24/7 availability
        total_hours_used = production_entries.count()
        actual_production = safe_aggregate(production_entries, 'quantity_produced', Sum, 0)
        
        theoretical_capacity = equipment.capacity_per_hour * total_hours_available
        capacity_utilization = (total_hours_used / max(total_hours_available, 1)) * 100
        efficiency_rate = (actual_production / max(theoretical_capacity, 1)) * 100
        
        # Future demand (pending work orders)
        pending_demand = WorkOrder.objects.filter(
            tenant=tenant,
            status__in=['planned', 'released'],
            product__in=Product.objects.filter(tenant=tenant)
        ).aggregate(Sum('quantity_planned'))['quantity_planned__sum'] or 0
        
        estimated_hours_needed = pending_demand / equipment.capacity_per_hour if equipment.capacity_per_hour > 0 else 0
        
        equipment_analysis.append({
            'equipment_code': equipment.equipment_code,
            'equipment_name': equipment.equipment_name,
            'capacity_per_hour': equipment.capacity_per_hour,
            'utilization_pct': round(capacity_utilization, 2),
            'efficiency_pct': round(efficiency_rate, 2),
            'actual_production': actual_production,
            'theoretical_capacity': theoretical_capacity,
            'pending_demand_hours': round(estimated_hours_needed, 2),
            'capacity_status': get_capacity_status(capacity_utilization, estimated_hours_needed, total_hours_available),
            'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}
        })
    
    return Response({
        'analysis_period_days': analysis_period,
        'equipment_analysis': equipment_analysis,
        'overall_capacity_utilization': sum(e['utilization_pct'] for e in equipment_analysis) / max(len(equipment_analysis), 1)
    })

# ===== INVENTORY MANAGEMENT =====

@api_view(['GET'])
def reorder_suggestions(request):
    """Purchase order suggestions based on stock levels"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    suggestions = generate_reorder_suggestions(tenant)
    
    # Calculate total investment required
    total_investment = sum(s['estimated_cost'] for s in suggestions)
    
    # Group by urgency
    critical_items = [s for s in suggestions if s['urgency'] == 'HIGH']
    medium_items = [s for s in suggestions if s['urgency'] == 'MEDIUM']
    
    return Response({
        'total_suggestions': len(suggestions),
        'critical_count': len(critical_items),
        'medium_count': len(medium_items),
        'total_investment_required': total_investment,
        'suggestions': suggestions,
        'summary': {
            'immediate_action_needed': len(critical_items),
            'plan_for_next_week': len(medium_items),
            'avg_cost_per_item': total_investment / max(len(suggestions), 1)
        }
    })

@api_view(['GET'])
def inventory_valuation(request):
    """Current inventory valuation report (robust to different output shapes)."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)

    valuation_date = request.query_params.get('as_of_date', timezone.now().date())
    if isinstance(valuation_date, str):
        try:
            valuation_date = datetime.strptime(valuation_date, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)

    # call util
    valuation_data_raw = calculate_inventory_valuation(tenant, valuation_date)

    # If util returned the wrapper {'total_inventory_value': X, 'details': {...}}, extract details.
    if isinstance(valuation_data_raw, dict) and 'details' in valuation_data_raw and isinstance(valuation_data_raw['details'], dict):
        valuation_data_raw = valuation_data_raw['details']

    # Normalize the result into a dict: { sku: { ... } }
    valuation_data = {}

    # Helper to canonicalize qty and total_value
    def _canonize_row(v):
        """Return a copy of v with canonical 'quantity' (float) and 'total_value' (float)."""
        qty = None
        # accept multiple possible names for quantity
        for k in ('quantity', 'current_qty', 'qty', 'on_hand', 'available_qty'):
            if isinstance(v, dict) and k in v and v.get(k) is not None:
                qty = v.get(k)
                break
        if qty is None:
            qty = 0

        # accept multiple possible names for total value
        total_value = None
        for k in ('total_value', 'value', 'inventory_value', 'amount'):
            if isinstance(v, dict) and k in v and v.get(k) is not None:
                total_value = v.get(k)
                break
        if total_value is None:
            # attempt to compute from qty*unit_cost if available
            unit_cost = None
            for k in ('unit_cost', 'average_cost', 'cost'):
                if isinstance(v, dict) and k in v and v.get(k) is not None:
                    unit_cost = v.get(k)
                    break
            try:
                if unit_cost is not None:
                    total_value = float(unit_cost) * float(qty)
                else:
                    total_value = 0.0
            except Exception:
                total_value = 0.0

        # convert Decimal or strings to float safely
        try:
            qty_f = float(qty)
        except Exception:
            qty_f = 0.0
        try:
            total_value_f = float(total_value)
        except Exception:
            total_value_f = 0.0

        # return a shallow copy with canonical fields
        if isinstance(v, dict):
            new = dict(v)
        else:
            new = {}
        new['quantity'] = qty_f
        new['total_value'] = total_value_f
        return new

    # Case 1: already a dict of sku -> row
    if isinstance(valuation_data_raw, dict):
        # but ensure every value is a dict and canonicalize keys
        for k, v in valuation_data_raw.items():
            if isinstance(v, dict):
                row = _canonize_row(v)
                valuation_data[str(k)] = row
            else:
                # scalar values: wrap into minimal dict
                try:
                    tv = float(v) if v is not None else 0.0
                except Exception:
                    tv = 0.0
                valuation_data[str(k)] = {'total_value': tv, 'quantity': 0.0}

    # Case 2: list/tuple of rows (try to extract sku)
    elif isinstance(valuation_data_raw, (list, tuple)):
        for item in valuation_data_raw:
            if not isinstance(item, dict):
                continue
            sku = item.get('sku') or item.get('product_sku') or item.get('code') or item.get('id')
            row = _canonize_row(item)
            if sku:
                valuation_data[str(sku)] = row
            else:
                # fallback: generate a synthetic key to keep the row
                synthetic = f"ROW_{len(valuation_data)+1}"
                valuation_data[synthetic] = row

    # Case 3: scalar (int/float/Decimal) — treat as total only
    elif isinstance(valuation_data_raw, (int, float, Decimal)):
        # put it under a synthetic key and proceed (keeps frontend from crashing)
        try:
            tv = float(valuation_data_raw)
        except Exception:
            tv = 0.0
        valuation_data['TOTAL'] = {
            'product_name': 'TOTAL',
            'quantity': 0.0,
            'unit_cost': 0.0,
            'total_value': tv
        }

    else:
        # Unexpected shape — log and return an error explaining the mismatch
        logger.error("calculate_inventory_valuation returned unexpected type: %s", type(valuation_data_raw))
        return Response({
            'error': 'Inventory valuation returned unexpected data shape',
            'details': f"Returned type: {type(valuation_data_raw).__name__}"
        }, status=500)

    # Now safe to compute summary metrics
    try:
        total_value = sum(float(v.get('total_value', 0) or 0) for v in valuation_data.values())
        total_quantity = sum(float(v.get('quantity', 0) or 0) for v in valuation_data.values())
    except Exception as e:
        logger.exception("Error while summarizing valuation_data: %s", e)
        return Response({'error': 'Failed to summarize valuation data', 'details': str(e)}, status=500)

    # Category-wise breakdown (try best-effort)
    category_breakdown = {}
    for sku, data in valuation_data.items():
        try:
            product = Product.objects.get(tenant=tenant, sku=sku)
            category = product.category or 'Uncategorized'
        except Product.DoesNotExist:
            category = data.get('category') or 'Uncategorized'

        if category not in category_breakdown:
            category_breakdown[category] = {'value': 0.0, 'quantity': 0.0, 'items': 0}

        category_breakdown[category]['value'] += float(data.get('total_value', 0) or 0)
        category_breakdown[category]['quantity'] += float(data.get('quantity', 0) or 0)
        category_breakdown[category]['items'] += 1

    return Response({
        'valuation_date': valuation_date,
        'total_inventory_value': total_value,
        'total_quantity': total_quantity,
        'item_count': len(valuation_data),
        'category_breakdown': category_breakdown,
        'detailed_valuation': valuation_data
    })

# ===== FINANCIAL ANALYSIS =====

# ===== AUTOMATED GL ENTRY =====
@api_view(['POST'])
def create_gl_entry(request):
    """Create automated GL entry (e.g., for production completion)"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    entry_type = request.data.get('entry_type')
    reference_data = request.data.get('reference_data')
    
    if not entry_type or not reference_data:
        return Response({'error': 'entry_type and reference_data required'}, status=400)
    
    try:
        journal_numbers = create_automated_gl_entry(tenant, entry_type, reference_data, user=request.user)
        return Response({
            'message': 'GL entry created successfully',
            'journal_numbers': journal_numbers
        }, status=201)
    except Exception as e:
        logger.error(f"GL entry creation failed: {str(e)}")
        return Response({'error': str(e)}, status=400)

@api_view(['GET'])
def profit_loss_statement(request):
    """Generate P&L statement with custom date range"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default (current month)
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        # Default to current month
        end_date = timezone.now().date()
        start_date = end_date.replace(day=1)
    
    # Get GL entries for the period
    gl_entries = GLJournalLine.objects.filter(
        tenant=tenant,
        journal__status='posted',
        journal__posting_date__range=[start_date, end_date]
    ).select_related('account')
    
    # Aggregate by account type
    revenue = gl_entries.filter(
        account__account_type='revenue'
    ).aggregate(
        total=Sum(F('credit_amount') - F('debit_amount'))
    )['total'] or 0
    
    cogs = gl_entries.filter(
        account__account_type='cogs'
    ).aggregate(
        total=Sum(F('debit_amount') - F('credit_amount'))
    )['total'] or 0
    
    expenses = gl_entries.filter(
        account__account_type='expense'
    ).aggregate(
        total=Sum(F('debit_amount') - F('credit_amount'))
    )['total'] or 0
    
    # Calculate derived figures
    gross_profit = revenue - cogs
    net_profit = gross_profit - expenses
    gross_margin = (gross_profit / max(revenue, 1)) * 100 if revenue > 0 else 0
    net_margin = (net_profit / max(revenue, 1)) * 100 if revenue > 0 else 0
    
    # Expense breakdown
    expense_breakdown = gl_entries.filter(
        account__account_type='expense'
    ).values('account__account_name').annotate(
        amount=Sum(F('debit_amount') - F('credit_amount'))
    ).order_by('-amount')[:10]
    
    return Response({
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'revenue': float(revenue),
        'cost_of_goods_sold': float(cogs),
        'gross_profit': float(gross_profit),
        'operating_expenses': float(expenses),
        'net_profit': float(net_profit),
        'gross_margin_pct': round(gross_margin, 2),
        'net_margin_pct': round(net_margin, 2),
        'expense_breakdown': list(expense_breakdown)
    })

@api_view(['GET'])
def cost_center_analysis(request):
    """Cost center performance analysis with custom date range"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    cost_centers = CostCenter.objects.filter(tenant=tenant, is_active=True)
    analysis_data = []
    
    for cc in cost_centers:
        performance = calculate_cost_center_performance(cc, start_date, end_date)
        
        # Additional metrics
        # Labor efficiency
        employees_in_cc = Employee.objects.filter(cost_center=cc, is_active=True)
        total_labor_hours = 0
        total_production = 0
        
        for employee in employees_in_cc:
            production_entries = ProductionEntry.objects.filter(
                tenant=tenant,
                operator=employee,
                entry_datetime__date__range=[start_date, end_date]
            )
            
            labor_hours = production_entries.count()  # 1 entry per hour
            production = safe_aggregate(production_entries, 'quantity_produced', Sum, 0)
            
            total_labor_hours += labor_hours
            total_production += production
        
        labor_productivity = total_production / max(total_labor_hours, 1)
        
        analysis_data.append({
            **performance,
            'labor_hours': total_labor_hours,
            'labor_productivity': round(labor_productivity, 2),
            'cost_efficiency_rating': get_cost_efficiency_rating(performance)
        })
    
    # Sort by total costs (highest first)
    analysis_data.sort(key=lambda x: x['total_costs'], reverse=True)
    
    return Response({
        'analysis_period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'cost_center_analysis': analysis_data,
        'total_cost_centers': len(analysis_data),
        'highest_cost_center': analysis_data[0]['cost_center_name'] if analysis_data else None
    })

# ===== QUALITY MANAGEMENT =====

@api_view(['GET'])
def rejection_analysis(request):
    """Quality rejection analysis and trends with custom date range"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    # Get production entries with rejections
    production_entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date],
        quantity_rejected__gt=0
    ).select_related('work_order__product', 'equipment', 'operator')
    
    # Rejection by product
    product_rejections = {}
    equipment_rejections = {}
    operator_rejections = {}
    daily_rejections = {}
    
    for entry in production_entries:
        # By product
        sku = entry.work_order.product.sku
        if sku not in product_rejections:
            product_rejections[sku] = {
                'product_name': entry.work_order.product.product_name,
                'total_produced': 0,
                'total_rejected': 0,
                'rejection_entries': 0
            }
        product_rejections[sku]['total_produced'] += entry.quantity_produced
        product_rejections[sku]['total_rejected'] += entry.quantity_rejected
        product_rejections[sku]['rejection_entries'] += 1
        
        # By equipment
        equip_name = entry.equipment.equipment_name
        if equip_name not in equipment_rejections:
            equipment_rejections[equip_name] = {'total_rejected': 0, 'entries': 0}
        equipment_rejections[equip_name]['total_rejected'] += entry.quantity_rejected
        equipment_rejections[equip_name]['entries'] += 1
        
        # By operator
        operator_name = entry.operator.full_name
        if operator_name not in operator_rejections:
            operator_rejections[operator_name] = {'total_rejected': 0, 'entries': 0}
        operator_rejections[operator_name]['total_rejected'] += entry.quantity_rejected
        operator_rejections[operator_name]['entries'] += 1
        
        # Daily trend
        date_key = entry.entry_datetime.date()
        if date_key not in daily_rejections:
            daily_rejections[date_key] = {'produced': 0, 'rejected': 0}
        daily_rejections[date_key]['produced'] += entry.quantity_produced
        daily_rejections[date_key]['rejected'] += entry.quantity_rejected
    
    # Calculate rejection rates
    for sku, data in product_rejections.items():
        total_output = data['total_produced'] + data['total_rejected']
        data['rejection_rate_pct'] = (data['total_rejected'] / max(total_output, 1)) * 100
    
    # Sort by rejection rate
    top_problem_products = sorted(
        product_rejections.items(),
        key=lambda x: x[1]['rejection_rate_pct'],
        reverse=True
    )[:5]
    
    # Daily trend data
    daily_trend = []
    for date_key in sorted(daily_rejections.keys()):
        data = daily_rejections[date_key]
        total_output = data['produced'] + data['rejected']
        rejection_rate = (data['rejected'] / max(total_output, 1)) * 100
        
        daily_trend.append({
            'date': date_key.isoformat(),
            'total_produced': data['produced'],
            'total_rejected': data['rejected'],
            'rejection_rate_pct': round(rejection_rate, 2)
        })
    
    return Response({
        'analysis_period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'summary': {
            'total_rejection_entries': production_entries.count(),
            'total_rejected_qty': sum(e.quantity_rejected for e in production_entries),
            'avg_daily_rejections': sum(d['rejected'] for d in daily_rejections.values()) / max(len(daily_rejections), 1)
        },
        'top_problem_products': top_problem_products,
        'equipment_performance': equipment_rejections,
        'operator_performance': operator_rejections,
        'daily_trend': daily_trend
    })

@api_view(['GET'])
def oee_trends(request):
    """OEE trends analysis across equipment with custom date range support"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 7)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    equipment_id = request.query_params.get('equipment_id')
    
    if equipment_id:
        equipment_list = [get_object_or_404(Equipment, id=equipment_id, tenant=tenant)]
    else:
        equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    
    trends_data = []
    
    for equipment in equipment_list:
        daily_oee = []
        current_date = start_date
        
        # Calculate all dates in the range
        date_range = []
        while current_date <= end_date:
            date_range.append(current_date)
            current_date += timedelta(days=1)
        
        for date in date_range:
            # Calculate OEE for this specific date
            oee_data = calculate_oee(equipment, date)
            
            daily_oee.append({
                'date': date.isoformat(),
                'oee': oee_data['oee'],
                'availability': oee_data['availability'],
                'performance': oee_data['performance'],
                'quality': oee_data['quality'],
                'hours_operated': oee_data['total_hours'],
                'total_produced': oee_data['total_produced'],
                'downtime_hours': oee_data['downtime_hours']
            })
        
        # Calculate averages only for days with data
        days_with_data = [d for d in daily_oee if d['hours_operated'] > 0]
        
        if days_with_data:
            avg_oee = sum(d['oee'] for d in days_with_data) / len(days_with_data)
            avg_availability = sum(d['availability'] for d in days_with_data) / len(days_with_data)
            avg_performance = sum(d['performance'] for d in days_with_data) / len(days_with_data)
            avg_quality = sum(d['quality'] for d in days_with_data) / len(days_with_data)
        else:
            avg_oee = avg_availability = avg_performance = avg_quality = 0
        
        trends_data.append({
            'equipment_id': equipment.id,
            'equipment_code': equipment.equipment_code,
            'equipment_name': equipment.equipment_name,
            'daily_oee_data': daily_oee,
            'avg_oee': round(avg_oee, 2),
            'avg_availability': round(avg_availability, 2),
            'avg_performance': round(avg_performance, 2),
            'avg_quality': round(avg_quality, 2),
            'trend_direction': get_trend_direction(daily_oee),
            'performance_rating': get_oee_rating(avg_oee)
        })
    
    return Response({
        'analysis_period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'equipment_trends': trends_data,
        'overall_avg_oee': sum(e['avg_oee'] for e in trends_data) / max(len(trends_data), 1) if trends_data else 0
    })

# ===== ANOMALY DETECTION =====
@api_view(['GET'])
def production_anomalies(request):
    """Detect production anomalies with custom date range"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    # Use the updated function that accepts date range
    anomalies = detect_production_anomalies_with_range(tenant, start_date, end_date)
    
    # Summarize
    high_severity_count = sum(1 for a in anomalies if a['severity'] == 'HIGH')
    medium_severity_count = sum(1 for a in anomalies if a['severity'] == 'MEDIUM')
    
    return Response({
        'analysis_period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'total_anomalies': len(anomalies),
        'high_severity': high_severity_count,
        'medium_severity': medium_severity_count,
        'anomalies': anomalies[:10],  # Limit for response size
        'recommendation': 'Review high-severity anomalies first for immediate action.'
    })


@api_view(['GET'])
def abc_analysis(request):
    """ABC analysis for inventory management (robust to different output shapes)."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)

    # If your utility accepts date or warehouse filters, pass them similarly here.
    valuation_raw = calculate_inventory_valuation(tenant)

    # Normalize into dict: { sku: { 'total_value': ..., 'quantity': ..., 'product_name': ... } }
    valuation = {}

    # Case 1: already a dict
    if isinstance(valuation_raw, dict):
        for k, v in valuation_raw.items():
            sku = str(k)
            if isinstance(v, dict):
                # Ensure numeric fields are floats (safe for sorting / JSON)
                total_value = v.get('total_value') if v.get('total_value') is not None else v.get('value') if v.get('value') is not None else 0
                try:
                    total_value = float(total_value)
                except Exception:
                    total_value = 0.0
                valuation[sku] = {
                    'product_name': v.get('product_name') or v.get('name') or sku,
                    'total_value': total_value,
                    'quantity': v.get('quantity') or 0
                }
            else:
                # scalar value -> wrap into a minimal dict
                try:
                    numeric = float(v)
                except Exception:
                    numeric = 0.0
                valuation[sku] = {
                    'product_name': sku,
                    'total_value': numeric,
                    'quantity': 0
                }

    # Case 2: list/tuple of row-dicts
    elif isinstance(valuation_raw, (list, tuple)):
        for row in valuation_raw:
            if not isinstance(row, dict):
                continue
            sku = row.get('sku') or row.get('product_sku') or row.get('code') or row.get('id')
            if not sku:
                # synthesize a key if no SKU present
                sku = f"ROW_{len(valuation) + 1}"
            total_value = row.get('total_value') or row.get('value') or 0
            try:
                total_value = float(total_value)
            except Exception:
                total_value = 0.0
            valuation[str(sku)] = {
                'product_name': row.get('product_name') or row.get('name') or sku,
                'total_value': total_value,
                'quantity': row.get('quantity') or 0
            }

    # Case 3: scalar (single total) — cannot perform ABC per SKU
    elif isinstance(valuation_raw, (int, float, Decimal)):
        logger.warning("calculate_inventory_valuation returned scalar for ABC analysis: %r", valuation_raw)
        return Response({
            'error': 'calculate_inventory_valuation returned a single numeric total, not per-SKU data.',
            'details': 'ABC analysis requires per-SKU valuation rows. Inspect calculate_inventory_valuation to return a dict or list of rows.'
        }, status=400)

    else:
        logger.error("calculate_inventory_valuation returned unexpected type for ABC: %s", type(valuation_raw))
        return Response({
            'error': 'Unexpected data shape returned from calculate_inventory_valuation',
            'type': type(valuation_raw).__name__
        }, status=500)

    # If after normalization we have nothing, return informative error
    if not valuation:
        logger.error("ABC normalization produced empty valuation_data (raw=%r)", valuation_raw)
        return Response({'error': 'No per-SKU valuation data available to run ABC analysis'}, status=400)

    # Now safe to proceed
    try:
        # Convert to list of tuples sorted by total_value descending
        sorted_items = sorted(
            valuation.items(),
            key=lambda x: float(x[1].get('total_value', 0) or 0),
            reverse=True
        )
    except Exception as e:
        logger.exception("Error sorting valuation data for ABC analysis: %s", e)
        return Response({'error': 'Failed to sort valuation data', 'details': str(e)}, status=500)

    total_value = sum(float(item[1].get('total_value', 0) or 0) for item in sorted_items)

    # If total_value is zero, classification is meaningless — return graceful message
    if total_value == 0:
        logger.warning("Total inventory value is zero for ABC analysis (tenant=%s).", getattr(tenant, 'id', str(tenant)))
        return Response({
            'total_items': len(sorted_items),
            'total_value': 0,
            'classification_summary': {'A': {'items': 0, 'value': 0}, 'B': {'items': 0, 'value': 0}, 'C': {'items': 0, 'value': 0}},
            'abc_analysis': [],
            'warning': 'Total inventory value is zero; ABC classification not performed.'
        })

    # Calculate ABC classification
    abc_analysis_list = []
    cumulative_value = 0.0

    for sku, data in sorted_items:
        value = float(data.get('total_value', 0) or 0)
        cumulative_value += value
        cumulative_pct = (cumulative_value / max(total_value, 1)) * 100

        if cumulative_pct <= 80:
            classification = 'A'
        elif cumulative_pct <= 95:
            classification = 'B'
        else:
            classification = 'C'

        abc_analysis_list.append({
            'sku': sku,
            'product_name': data.get('product_name', sku),
            'inventory_value': value,
            'quantity': data.get('quantity', 0),
            'classification': classification,
            'cumulative_value_pct': round(cumulative_pct, 2)
        })

    # Summary by classification
    classification_summary = {'A': {'items': 0, 'value': 0.0}, 'B': {'items': 0, 'value': 0.0}, 'C': {'items': 0, 'value': 0.0}}
    for item in abc_analysis_list:
        cls = item['classification']
        classification_summary[cls]['items'] += 1
        classification_summary[cls]['value'] += float(item['inventory_value'] or 0)

    return Response({
        'total_items': len(abc_analysis_list),
        'total_value': total_value,
        'classification_summary': classification_summary,
        'abc_analysis': abc_analysis_list
    })

@api_view(['GET'])
def category_valuation_detail(request, category_name):
    """Get detailed valuation for a specific category"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    valuation_date = request.query_params.get('as_of_date', timezone.now().date())
    if isinstance(valuation_date, str):
        try:
            valuation_date = datetime.strptime(valuation_date, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    # Get all products in this category
    products = Product.objects.filter(
        tenant=tenant, 
        category=category_name if category_name != 'Uncategorized' else None,
        is_active=True
    )
    
    # Get valuation data
    valuation_data = calculate_inventory_valuation(tenant, valuation_date)
    detailed_valuation = valuation_data.get('details', {})
    
    # Filter products in this category
    category_products = []
    total_value = 0
    total_quantity = 0
    low_stock_count = 0
    out_of_stock_count = 0
    
    for product in products:
        product_data = detailed_valuation.get(product.sku, {})
        if product_data:
            category_products.append({
                'sku': product.sku,
                'product_name': product.product_name,
                'quantity': product_data.get('current_qty', 0),
                'unit_cost': product_data.get('average_cost', 0),
                'total_value': product_data.get('total_value', 0),
                'reorder_point': product.reorder_point
            })
            
            total_value += product_data.get('total_value', 0)
            total_quantity += product_data.get('current_qty', 0)
            
            if product_data.get('current_qty', 0) <= 0:
                out_of_stock_count += 1
            elif product_data.get('current_qty', 0) <= product.reorder_point:
                low_stock_count += 1
    
    # Get top products by value
    top_products = sorted(category_products, key=lambda x: x['total_value'], reverse=True)[:10]
    
    return Response({
        'category': category_name,
        'total_value': total_value,
        'total_quantity': total_quantity,
        'product_count': len(category_products),
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'avg_value_per_item': total_value / max(len(category_products), 1),
        'top_products': top_products,
        'products': category_products,
        'valuation_date': valuation_date
    })

# ===== FINANCIAL SUMMARY =====
@api_view(['GET'])
def financial_summary(request):
    """Financial summary report with custom date range"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)

    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)

    summary = generate_financial_summary(tenant, start_date, end_date)

    return Response({
        'summary': summary,
        'profitability_status': 'PROFITABLE' if summary['net_profit'] > 0 else 'LOSS_MAKING'
    })

# ===== DASHBOARD OVERVIEW =====
@api_view(['GET'])
def business_overview_dashboard(request):
    """Comprehensive business overview for dashboard with custom date range"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Parse date range or use default
    start_date, end_date = parse_date_range(request, 7)
    if start_date is None:
        return Response({'error': 'Invalid date format (use YYYY-MM-DD)'}, status=400)
    
    dashboard_data = {
        'date_generated': timezone.now(),
        'period_covered': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}
    }
    
    # 1. Production Summary
    production_summary = get_production_summary(tenant, start_date, end_date)
    dashboard_data['production_summary'] = production_summary
    
    # 2. Upcoming Work Orders (planned but not started, due soon)
    upcoming_work_orders = get_upcoming_work_orders(tenant)
    dashboard_data['upcoming_work_orders'] = upcoming_work_orders
    
    # 3. Inventory Status
    inventory_status = get_inventory_status(tenant)
    dashboard_data['inventory_status'] = inventory_status
    
    # 4. Financial Highlights
    financial_highlights = get_financial_highlights(tenant, start_date, end_date)
    dashboard_data['financial_highlights'] = financial_highlights
    
    # 5. Quality Metrics
    quality_metrics = get_quality_metrics(tenant, start_date, end_date)
    dashboard_data['quality_metrics'] = quality_metrics
    
    # 6. Equipment Status
    equipment_status = get_equipment_status(tenant)
    dashboard_data['equipment_status'] = equipment_status
    
    # 7. Recent Activities
    recent_activities = get_recent_activities(tenant)
    dashboard_data['recent_activities'] = recent_activities
    
    # 8. Key Performance Indicators
    kpis = get_key_performance_indicators(tenant, start_date, end_date)
    dashboard_data['key_performance_indicators'] = kpis

    # 9. Production Trends Data
    production_trends_data = get_production_trends_data(tenant, start_date, end_date)
    dashboard_data['production_trends'] = production_trends_data
    
    # 10. Financial Trends Data
    financial_trends_data = get_financial_trends_data(tenant, start_date, end_date)
    dashboard_data['financial_trends'] = financial_trends_data
    
    return Response(dashboard_data)

# ===== HELPER FUNCTIONS =====

# Add these helper functions to generate trend data
def get_production_trends_data(tenant, start_date, end_date):
    """Get production trends data for charts"""
    daily_data = []
    current_date = start_date
    
    while current_date <= end_date:
        # Get production for this day
        day_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            entry_datetime__date=current_date
        )
        
        produced = day_entries.aggregate(total=Sum('quantity_produced'))['total'] or 0
        rejected = day_entries.aggregate(total=Sum('quantity_rejected'))['total'] or 0
        
        # Calculate target based on equipment capacity
        total_capacity = Equipment.objects.filter(
            tenant=tenant, is_active=True
        ).aggregate(total=Sum('capacity_per_hour'))['total'] or 0
        target = total_capacity * 8  # Assuming 8-hour shift
        
        daily_data.append({
            'date': current_date.strftime('%b %d'),
            'produced': produced,
            'rejected': rejected,
            'target': target
        })
        
        current_date += timedelta(days=1)
    
    return {
        'daily_data': daily_data,
        'total_produced': sum(item['produced'] for item in daily_data),
        'total_rejected': sum(item['rejected'] for item in daily_data)
    }

def get_financial_trends_data(tenant, start_date, end_date):
    """Get financial trends data for charts"""
    weekly_data = []
    current_date = start_date
    
    # Ensure we have at least 4 data points for a good chart
    days_in_period = (end_date - start_date).days
    interval_days = max(1, days_in_period // 4)
    
    while current_date <= end_date:
        period_end = min(current_date + timedelta(days=interval_days), end_date)
        
        # Get financial data for this period
        gl_entries = GLJournalLine.objects.filter(
            tenant=tenant,
            journal__status='posted',
            journal__posting_date__range=[current_date, period_end]
        )
        
        revenue = gl_entries.filter(
            account__account_type='revenue'
        ).aggregate(total=Sum(F('credit_amount') - F('debit_amount')))['total'] or 0
        
        expenses = gl_entries.filter(
            account__account_type='expense'
        ).aggregate(total=Sum(F('debit_amount') - F('credit_amount')))['total'] or 0
        
        profit = revenue - expenses
        
        weekly_data.append({
            'date': current_date.strftime('%b %d'),
            'revenue': float(revenue),
            'expenses': float(expenses),
            'profit': float(profit)
        })
        
        current_date += timedelta(days=interval_days + 1)
    
    return {
        'period_data': weekly_data,
        'total_revenue': sum(item['revenue'] for item in weekly_data),
        'total_expenses': sum(item['expenses'] for item in weekly_data),
        'total_profit': sum(item['profit'] for item in weekly_data)
    }
    
def calculate_urgency_score(work_order):
    """Calculate urgency score for work order prioritization"""
    today = timezone.now().date()
    days_to_due = (work_order.due_date - today).days
    
    # Base urgency on due date proximity
    if days_to_due <= 0:
        urgency = 100  # Overdue
    elif days_to_due <= 1:
        urgency = 90   # Due tomorrow
    elif days_to_due <= 3:
        urgency = 70   # Due in 3 days
    elif days_to_due <= 7:
        urgency = 50   # Due in a week
    else:
        urgency = 20   # Future
    
    # Adjust based on priority (1=High, 10=Low)
    priority_adjustment = (11 - work_order.priority) * 5
    urgency += priority_adjustment
    
    return min(100, urgency)

def get_bottleneck_equipment(equipment_schedule):
    """Identify bottleneck equipment"""
    bottlenecks = []
    for equip in equipment_schedule:
        if equip['current_utilization_pct'] > 85:  # High utilization threshold
            bottlenecks.append({
                'equipment_name': equip['equipment_name'],
                'utilization_pct': equip['current_utilization_pct'],
                'pending_orders': len(equip['recommended_orders'])
            })
    
    return sorted(bottlenecks, key=lambda x: x['utilization_pct'], reverse=True)

def get_capacity_status(utilization_pct, pending_hours, available_hours):
    """Determine capacity status"""
    if utilization_pct > 90:
        return 'OVERLOADED'
    elif utilization_pct > 75:
        return 'HIGH_UTILIZATION'
    elif pending_hours > available_hours * 0.8:
        return 'FUTURE_BOTTLENECK'
    else:
        return 'AVAILABLE'

def get_cost_efficiency_rating(performance_data):
    """Rate cost center efficiency"""
    cost_per_unit = performance_data.get('cost_per_unit', 0)
    budget_variance = performance_data.get('budget_variance_pct', 0)
    
    if cost_per_unit <= 0:
        return 'UNKNOWN'
    
    if budget_variance < -10:  # Over budget by more than 10%
        return 'POOR'
    elif budget_variance < 0:  # Slightly over budget
        return 'FAIR'
    elif budget_variance <= 5:  # On or under budget
        return 'GOOD'
    else:  # Significantly under budget
        return 'EXCELLENT'

def get_trend_direction(daily_data):
    """Determine trend direction from daily data"""
    if len(daily_data) < 2:
        return 'INSUFFICIENT_DATA'
    
    # Get first and last OEE values
    first_oee = daily_data[0]['oee']
    last_oee = daily_data[-1]['oee']
    
    if last_oee > first_oee + 5:
        return 'IMPROVING'
    elif last_oee < first_oee - 5:
        return 'DECLINING'
    else:
        return 'STABLE'

def get_oee_rating(oee_pct):
    """Rate OEE performance"""
    if oee_pct >= 85:
        return 'WORLD_CLASS'
    elif oee_pct >= 75:
        return 'EXCELLENT'
    elif oee_pct >= 65:
        return 'GOOD'
    elif oee_pct >= 50:
        return 'FAIR'
    else:
        return 'POOR'

def detect_production_anomalies_with_range(tenant, start_date, end_date):
    """Detect production anomalies within a date range"""
    anomalies = []
    
    # Get production entries in the date range
    production_entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date]
    ).select_related('work_order__product', 'equipment', 'operator')
    
    # Group by equipment and hour to find anomalies
    equipment_hourly_data = {}
    
    for entry in production_entries:
        hour_key = f"{entry.equipment.id}_{entry.entry_datetime.hour}"
        
        if hour_key not in equipment_hourly_data:
            equipment_hourly_data[hour_key] = {
                'equipment': entry.equipment,
                'hour': entry.entry_datetime.hour,
                'entries': [],
                'total_produced': 0,
                'total_rejected': 0
            }
        
        equipment_hourly_data[hour_key]['entries'].append(entry)
        equipment_hourly_data[hour_key]['total_produced'] += entry.quantity_produced
        equipment_hourly_data[hour_key]['total_rejected'] += entry.quantity_rejected
    
    # Analyze each hour for anomalies
    for hour_data in equipment_hourly_data.values():
        equipment = hour_data['equipment']
        expected_production = equipment.capacity_per_hour
        
        # Check for low production
        if hour_data['total_produced'] < expected_production * 0.5:
            anomalies.append({
                'type': 'LOW_PRODUCTION',
                'equipment': equipment.equipment_name,
                'hour': hour_data['hour'],
                'expected': expected_production,
                'actual': hour_data['total_produced'],
                'severity': 'HIGH' if hour_data['total_produced'] < expected_production * 0.3 else 'MEDIUM',
                'suggestion': 'Check equipment for maintenance needs or operator training'
            })
        
        # Check for high rejection rate
        total_output = hour_data['total_produced'] + hour_data['total_rejected']
        if total_output > 0:
            rejection_rate = (hour_data['total_rejected'] / total_output) * 100
            if rejection_rate > 10:  # More than 10% rejection
                anomalies.append({
                    'type': 'HIGH_REJECTION',
                    'equipment': equipment.equipment_name,
                    'hour': hour_data['hour'],
                    'rejection_rate': round(rejection_rate, 2),
                    'severity': 'HIGH' if rejection_rate > 20 else 'MEDIUM',
                    'suggestion': 'Inspect quality control processes and material quality'
                })
    
    return sorted(anomalies, key=lambda x: 0 if x['severity'] == 'HIGH' else 1)

# Dashboard helper functions
def get_production_summary(tenant, start_date, end_date):
    """Get production summary for the period"""
    # Total production entries
    production_entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date]
    )
    
    total_entries = production_entries.count()
    total_produced = production_entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
    total_rejected = production_entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    
    # Active work orders
    active_work_orders = WorkOrder.objects.filter(
        tenant=tenant,
        status__in=['released', 'in_progress'],
        is_active=True
    ).count()
    
    # Completed work orders in period
    completed_work_orders = WorkOrder.objects.filter(
        tenant=tenant,
        status='completed',
        updated_at__date__range=[start_date, end_date],
        is_active=True
    ).count()
    
    # Calculate efficiency (tolerate different utils signatures and return types)
    days = max((end_date - start_date).days, 1)
    efficiency_data = None
    try:
        # Try signature: (tenant, days)
        efficiency_data = get_production_efficiency_trends(tenant, days)
    except TypeError:
        # Fallback: (tenant)
        efficiency_data = get_production_efficiency_trends(tenant)

    def _coerce_avg_efficiency(data):
        # If already a number
        if isinstance(data, (int, float)):
            return float(data)

        # If it's a dict: look for common keys
        if isinstance(data, dict):
            for k in ('avg_efficiency', 'efficiency', 'value', 'oee', 'rate'):
                v = data.get(k)
                if isinstance(v, (int, float)):
                    return float(v)
            return 0.0

        # If it's a list/tuple: average any numeric fields on each item
        if isinstance(data, (list, tuple)):
            vals = []
            for item in data:
                if isinstance(item, (int, float)):
                    vals.append(float(item))
                elif isinstance(item, dict):
                    for k in ('avg_efficiency', 'efficiency', 'value', 'oee', 'rate'):
                        v = item.get(k)
                        if isinstance(v, (int, float)):
                            vals.append(float(v))
                            break  # take first matching key for this item
            return round(sum(vals) / len(vals), 2) if vals else 0.0

        # Unknown type
        return 0.0

    avg_efficiency = _coerce_avg_efficiency(efficiency_data)

    return {
        'total_produced': total_produced,
        'total_rejected': total_rejected,
        'rejection_rate': (total_rejected / max(total_produced + total_rejected, 1)) * 100,
        'active_work_orders': active_work_orders,
        'completed_work_orders': completed_work_orders,
        'production_entries': total_entries,
        'avg_efficiency': round(avg_efficiency, 2),
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}
    }


def get_upcoming_work_orders(tenant):
    """Get work orders that are planned but not started, with due dates approaching"""
    today = timezone.now().date()
    next_week = today + timedelta(days=7)
    
    # Get planned work orders due in the next week
    upcoming_orders = WorkOrder.objects.filter(
        tenant=tenant,
        status='planned',
        due_date__lte=next_week,
        due_date__gte=today,
        is_active=True
    ).select_related('product', 'cost_center').order_by('due_date', 'priority')[:10]  # Limit to 10 most urgent
    
    orders_data = []
    for order in upcoming_orders:
        days_until_due = (order.due_date - today).days
        urgency = "HIGH" if days_until_due <= 2 else "MEDIUM" if days_until_due <= 5 else "LOW"
        
        orders_data.append({
            'wo_number': order.wo_number,
            'product_sku': order.product.sku,
            'product_name': order.product.product_name,
            'quantity_planned': order.quantity_planned,
            'due_date': order.due_date.isoformat(),
            'days_until_due': days_until_due,
            'priority': order.priority,
            'urgency': urgency,
            'cost_center': order.cost_center.name
        })
    
    return {
        'count': len(orders_data),
        'high_urgency_count': sum(1 for o in orders_data if o['urgency'] == 'HIGH'),
        'orders': orders_data
    }

# --- small internal helpers (scoped in this module) ---

def _extract_total_value(x):
    """
    Accept a scalar total, a dict with common total keys, or derive qty * unit cost.
    Returns a float (0.0 if unknown).
    """
    if isinstance(x, (int, float)):
        return float(x)

    if isinstance(x, dict):
        # direct total fields
        for k in ('total_value', 'value', 'inventory_value', 'amount'):
            v = x.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        # derive from qty * unit price/cost
        qty_keys = ('qty', 'quantity', 'on_hand', 'stock', 'balance')
        price_keys = ('unit_price', 'price', 'avg_cost', 'cost')
        q = next((x.get(k) for k in qty_keys if isinstance(x.get(k), (int, float))), None)
        p = next((x.get(k) for k in price_keys if isinstance(x.get(k), (int, float))), None)
        if q is not None and p is not None:
            return float(q) * float(p)

    return 0.0


def _iter_valuation_items(valuation_data):
    """
    Yields (sku, data_dict) pairs for downstream logic that expects per-SKU entries.
    Supports dict-of-dicts, list-of-dicts (with 'sku'), or anything else (yields none).
    """
    if isinstance(valuation_data, dict):
        for sku, data in valuation_data.items():
            if isinstance(data, dict):
                yield sku, data
    elif isinstance(valuation_data, (list, tuple)):
        for item in valuation_data:
            if isinstance(item, dict):
                sku = item.get('sku') or item.get('product_sku') or item.get('code')
                if sku:
                    yield sku, item


def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


def _to_iso(dt):
    try:
        return dt.isoformat()
    except Exception:
        return None


def get_inventory_status(tenant):
    """Get inventory status summary"""
    # Get current inventory valuation (can be: dict, list, or scalar)
    valuation_data = calculate_inventory_valuation(tenant)

    # Total value tolerant to shapes
    if isinstance(valuation_data, dict):
        total_value = sum(_extract_total_value(v) for v in valuation_data.values())
        total_items = len(valuation_data)
    elif isinstance(valuation_data, (list, tuple)):
        total_value = sum(_extract_total_value(v) for v in valuation_data)
        # Count distinct SKUs when possible
        skus = { (item.get('sku') or item.get('product_sku') or item.get('code'))
                 for item in valuation_data if isinstance(item, dict) and (
                     item.get('sku') or item.get('product_sku') or item.get('code')) }
        total_items = len(skus) if skus else len(valuation_data)
    else:
        total_value = _extract_total_value(valuation_data)
        total_items = 1 if total_value else 0

    # Check for low stock items (only possible if we have per-SKU rows)
    low_stock_items = []
    for sku, data in _iter_valuation_items(valuation_data):
        try:
            product = Product.objects.get(tenant=tenant, sku=sku)
        except Product.DoesNotExist:
            continue

        qty = data.get('quantity') or data.get('qty') or data.get('on_hand') or 0
        try:
            qty = float(qty)
        except Exception:
            qty = 0

        # Treat missing reorder_point as 0 (never low) unless explicitly set
        rp = getattr(product, 'reorder_point', 0) or 0

        if 0 < qty <= rp:
            urgency = 'CRITICAL' if rp > 0 and qty <= (rp * 0.3) else 'WARNING'
            low_stock_items.append({
                'sku': sku,
                'product_name': data.get('product_name') or getattr(product, 'product_name', sku),
                'current_stock': qty,
                'reorder_point': rp,
                'urgency': urgency
            })

    # Get recent stock movements (last 2 days)
    recent_movements = (StockMovement.objects
                        .filter(tenant=tenant, movement_date__gte=timezone.now() - timedelta(days=2))
                        .select_related('product', 'warehouse')
                        .order_by('-movement_date')[:5])

    movements_data = []
    for movement in recent_movements:
        movements_data.append({
            'movement_type': movement.movement_type,
            'product_sku': getattr(movement.product, 'sku', None),
            'product_name': getattr(movement.product, 'product_name', None),
            'quantity': movement.quantity,
            'warehouse': getattr(movement.warehouse, 'warehouse_name', None),
            'movement_date': _to_iso(movement.movement_date),
        })

    return {
        'total_inventory_value': float(total_value),
        'total_items': _safe_int(total_items),
        'low_stock_items_count': len(low_stock_items),
        'low_stock_items': low_stock_items[:5],  # Top 5 most critical
        'recent_movements': movements_data,
    }


def get_financial_highlights(tenant, start_date, end_date):
    """Get financial highlights for the period"""
    # Generate P&L for the period
    gl_entries = (GLJournalLine.objects
                  .filter(
                      tenant=tenant,
                      journal__status='posted',
                      journal__posting_date__range=[start_date, end_date]
                  )
                  .select_related('account'))

    # Calculate key financial metrics
    revenue = (gl_entries.filter(account__account_type='revenue')
               .aggregate(total=Sum(F('credit_amount') - F('debit_amount')))['total'] or 0)

    cogs = (gl_entries.filter(account__account_type='cogs')
            .aggregate(total=Sum(F('debit_amount') - F('credit_amount')))['total'] or 0)

    expenses = (gl_entries.filter(account__account_type='expense')
                .aggregate(total=Sum(F('debit_amount') - F('credit_amount')))['total'] or 0)

    gross_profit = revenue - cogs
    net_profit = gross_profit - expenses

    # Get top expenses
    top_expenses_qs = (gl_entries.filter(account__account_type='expense')
                       .values('account__account_name')
                       .annotate(amount=Sum(F('debit_amount') - F('credit_amount')))
                       .order_by('-amount')[:5])

    # Normalize top_expenses to stable keys
    top_expenses = [{'account_name': row['account__account_name'], 'amount': float(row['amount'] or 0)}
                    for row in top_expenses_qs]

    return {
        'revenue': float(revenue),
        'cost_of_goods_sold': float(cogs),
        'gross_profit': float(gross_profit),
        'operating_expenses': float(expenses),
        'net_profit': float(net_profit),
        'gross_margin': (float(gross_profit) / float(revenue)) * 100 if revenue else 0.0,
        'net_margin': (float(net_profit) / float(revenue)) * 100 if revenue else 0.0,
        'top_expenses': top_expenses,
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
    }


def get_quality_metrics(tenant, start_date, end_date):
    """Get quality metrics for dashboard"""
    production_entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date]
    )
    
    total_produced = safe_aggregate(production_entries, 'quantity_produced', Sum, 0)
    total_rejected = safe_aggregate(production_entries, 'quantity_rejected', Sum, 0)
    total_output = total_produced + total_rejected
    
    rejection_rate = (total_rejected / max(total_output, 1)) * 100
    
    # Get top 3 products with highest rejection rates
    product_rejections = production_entries.values(
        'work_order__product__sku', 'work_order__product__product_name'
    ).annotate(
        total_produced=Sum('quantity_produced'),
        total_rejected=Sum('quantity_rejected')
    ).filter(total_rejected__gt=0).order_by('-total_rejected')[:3]
    
    top_rejections = []
    for pr in product_rejections:
        total = pr['total_produced'] + pr['total_rejected']
        rate = (pr['total_rejected'] / max(total, 1)) * 100
        top_rejections.append({
            'sku': pr['work_order__product__sku'],
            'product_name': pr['work_order__product__product_name'],
            'rejection_rate_pct': round(rate, 2)
        })
    
    return {
        'overall_rejection_rate_pct': round(rejection_rate, 2),
        'top_rejections': top_rejections
    }

def get_equipment_status(tenant):
    """Get equipment status summary"""
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)

    status_summary = {
        'total_equipment': equipment_list.count(),
        'equipment_details': []
    }

    today = timezone.now().date()

    for equipment in equipment_list:
        # next_maintenance may be date or datetime or None
        nm = getattr(equipment, 'next_maintenance', None)
        nm_date = nm.date() if hasattr(nm, 'date') else nm

        # Check maintenance status
        maintenance_status = "OK"
        if nm_date:
            if nm_date <= today:
                maintenance_status = "MAINTENANCE_NEEDED"
            elif nm_date <= (today + timedelta(days=7)):
                maintenance_status = "MAINTENANCE_SOON"

        # Recent production in last 24h
        recent_production = (ProductionEntry.objects
                             .filter(
                                 tenant=tenant,
                                 equipment=equipment,
                                 entry_datetime__gte=timezone.now() - timedelta(days=1)
                             )
                             .aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0)

        status_summary['equipment_details'].append({
            'equipment_code': equipment.equipment_code,
            'equipment_name': equipment.equipment_name,
            'maintenance_status': maintenance_status,
            'next_maintenance': _to_iso(nm) if nm else None,
            'recent_production': recent_production,
            'status': 'ACTIVE' if recent_production > 0 else 'IDLE'
        })

    return status_summary

def get_recent_activities(tenant):
    """Get recent activities for dashboard"""
    recent_entries = ProductionEntry.objects.filter(
        tenant=tenant
    ).select_related('work_order__product', 'equipment').order_by('-entry_datetime')[:5]
    
    return [
        {
            'product_sku': entry.work_order.product.sku,
            'equipment': entry.equipment.equipment_name,
            'quantity_produced': entry.quantity_produced,
            'timestamp': entry.entry_datetime.isoformat()
        }
        for entry in recent_entries
    ]

def get_key_performance_indicators(tenant, start_date, end_date):
    """Calculate key performance indicators"""
    # OEE (Overall Equipment Effectiveness)
    equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
    total_oee = 0.0
    equipment_count = 0

    for equipment in equipment_list:
        entries = ProductionEntry.objects.filter(
            tenant=tenant,
            equipment=equipment,
            entry_datetime__date__range=[start_date, end_date]
        )

        if entries.exists():
            total_produced = entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
            total_rejected = entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
            total_downtime = entries.aggregate(Sum('downtime_minutes'))['downtime_minutes__sum'] or 0

            # Treat each entry as an "hour" (your existing simplification)
            hours_operated = entries.count()

            # Availability
            total_minutes = hours_operated * 60
            availability = ((total_minutes - (total_downtime or 0)) / total_minutes * 100) if total_minutes > 0 else 0.0

            # Performance: produced / theoretical capacity
            theoretical_output = (getattr(equipment, 'capacity_per_hour', 0) or 0) * hours_operated
            performance = ((total_produced or 0) / theoretical_output * 100) if theoretical_output > 0 else 0.0

            # Quality
            total_output = (total_produced or 0) + (total_rejected or 0)
            quality = ((total_produced or 0) / total_output * 100) if total_output > 0 else 0.0

            # OEE (A * P * Q) / 10000 (since A,P,Q in %)
            oee = (availability * performance * quality) / 10000.0
            total_oee += oee
            equipment_count += 1

    avg_oee = (total_oee / equipment_count) if equipment_count > 0 else 0.0

    # Production efficiency (rejection rate)
    production_entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date]
    )

    total_produced = production_entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
    total_rejected = production_entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    total_output = (total_produced or 0) + (total_rejected or 0)
    rejection_rate = ((total_rejected or 0) / total_output * 100) if total_output > 0 else 0.0

    # On-time delivery (simplified - assuming completed WOs were delivered)
    completed_orders = WorkOrder.objects.filter(
        tenant=tenant,
        status='completed',
        updated_at__date__range=[start_date, end_date]
    )

    completed_count = completed_orders.count()
    on_time_orders = completed_orders.filter(due_date__gte=F('updated_at__date'))
    on_time_delivery = (on_time_orders.count() / completed_count * 100) if completed_count > 0 else 0.0

    return {
        'overall_equipment_effectiveness': round(avg_oee, 2),
        'rejection_rate': round(rejection_rate, 2),
        'on_time_delivery_rate': round(on_time_delivery, 2),
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
    }

# ===== MATERIAL CONSUMPTION ANALYSIS =====
@api_view(['GET'])
def material_consumption_report(request, wo_id=None):
    """Material consumption report for a work order"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    if wo_id:
        work_order = get_object_or_404(WorkOrder, id=wo_id, tenant=tenant)
        consumption_data = calculate_material_consumption(work_order)
        
        # Calculate totals
        total_consumed = sum(item['actual_consumed'] for item in consumption_data.values())
        total_cost = sum(item['total_cost'] for item in consumption_data.values())
        
        return Response({
            'work_order': work_order.wo_number,
            'consumption_data': consumption_data,
            'total_consumed': total_consumed,
            'total_cost': total_cost,
            'avg_cost_per_unit': total_cost / max(total_consumed, 1)
        })
    else:
        return Response({'error': 'Work order ID required'}, status=400)

# ===== DASHBOARD ALERTS =====
@api_view(['GET'])
def dashboard_alerts(request):
    """Real-time business alerts for dashboard"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    alerts = get_dashboard_alerts(tenant)
    
    # Summarize
    critical_alerts = sum(1 for a in alerts if a['severity'] == 'HIGH')
    
    return Response({
        'total_alerts': len(alerts),
        'critical_alerts': critical_alerts,
        'alerts': alerts
    })

@api_view(['GET'])
def overdue_work_orders(request):
    """Get overdue work orders"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    today = timezone.now().date()
    
    overdue_orders = WorkOrder.objects.filter(
        tenant=tenant,
        due_date__lt=today,
        status__in=['planned', 'released', 'in_progress'],
        is_active=True
    ).select_related('product', 'cost_center').order_by('due_date', 'priority')
    
    orders_data = []
    for order in overdue_orders:
        days_overdue = (today - order.due_date).days
        
        orders_data.append({
            'wo_number': order.wo_number,
            'product_sku': order.product.sku,
            'product_name': order.product.product_name,
            'quantity_planned': order.quantity_planned,
            'quantity_completed': order.quantity_completed,
            'due_date': order.due_date.isoformat(),
            'days_overdue': days_overdue,
            'status': order.status,
            'priority': order.priority,
            'cost_center': order.cost_center.name
        })
    
    return Response({
        'count': len(orders_data),
        'orders': orders_data
    })