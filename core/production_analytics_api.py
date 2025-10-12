# production_analytics_api.py - COMPLETELY FIXED VERSION
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.db.models import Sum, Count
from django.utils import timezone
from datetime import datetime, timedelta
import logging
from django.core.cache import cache

from .middleware import get_current_tenant
from .models import WorkOrder, ProductionEntry, Equipment, Employee, Product, CostCenter

logger = logging.getLogger(__name__)

# ---------- Helpers ----------

def parse_date_range(request, default_days=30):
    """Returns (start_date, end_date) as date objects. Validates format YYYY-MM-DD."""
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
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        return start_date, end_date
    except Exception:
        return None, None


def safe_div(num, den):
    try:
        return float(num) / float(den) if den not in (0, None) else 0.0
    except Exception:
        return 0.0


def round2(v):
    try:
        return round(float(v), 2)
    except Exception:
        return 0.0


# ---------- Core Aggregation Functions ----------

def _production_entries_qs(tenant, start_date, end_date, extra_filters=None):
    qs = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date]
    ).select_related('work_order', 'equipment', 'operator', 'work_order__product', 'work_order__cost_center')
    if extra_filters:
        qs = qs.filter(**extra_filters)
    return qs


# ---------- Response Builders ----------

def build_entry_dict(entry):
    """Build a complete production entry dictionary"""
    return {
        'id': entry.id,
        'work_order_id': entry.work_order.id if entry.work_order else None,
        'wo_number': entry.work_order.wo_number if entry.work_order else None,
        'product_sku': entry.work_order.product.sku if entry.work_order and entry.work_order.product else None,
        'product_name': entry.work_order.product.product_name if entry.work_order and entry.work_order.product else None,
        'equipment_id': entry.equipment.id if entry.equipment else None,
        'equipment_code': entry.equipment.equipment_code if entry.equipment else None,
        'equipment_name': entry.equipment.equipment_name if entry.equipment else None,
        'operator_id': entry.operator.id if entry.operator else None,
        'operator_name': entry.operator.full_name if entry.operator else None,
        'entry_datetime': entry.entry_datetime.isoformat() if entry.entry_datetime else None,
        'quantity_produced': float(entry.quantity_produced or 0),
        'quantity_rejected': float(entry.quantity_rejected or 0),
        'downtime_minutes': float(entry.downtime_minutes or 0),
        'downtime_reason': entry.downtime_reason if hasattr(entry, 'downtime_reason') else '',
        'shift': entry.shift if hasattr(entry, 'shift') else '',
    }


# ---------- Endpoints ----------

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def production_summary(request):
    """Tenant-wide production summary across the requested date range."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)

    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=400)

    cache_key = f"prod_summary:{tenant.id}:{start_date}:{end_date}"
    cached = cache.get(cache_key)
    if cached:
        return Response(cached)

    entries = _production_entries_qs(tenant, start_date, end_date)

    total_entries = entries.count()
    total_produced = entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
    total_rejected = entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    total_downtime = entries.aggregate(Sum('downtime_minutes'))['downtime_minutes__sum'] or 0

    distinct_work_orders = entries.values('work_order_id').distinct().count()
    distinct_products = entries.values('work_order__product_id').distinct().count()
    distinct_equipment = entries.values('equipment_id').distinct().count()
    distinct_operators = entries.values('operator_id').distinct().count()

    # Quality and yield
    yield_pct = safe_div(total_produced, (total_produced + total_rejected)) * 100
    avg_hourly_output = safe_div(total_produced, max(total_entries, 1))

    # Top performers
    by_operator = entries.values('operator_id', 'operator__full_name').annotate(
        produced=Sum('quantity_produced'), rejected=Sum('quantity_rejected'), entries=Count('id')
    ).order_by('-produced')[:10]

    top_operators = [
        {
            'operator_id': o['operator_id'],
            'operator_name': o['operator__full_name'],
            'produced': float(o['produced'] or 0),
            'rejected': float(o['rejected'] or 0),
            'entries': int(o['entries']),
            'avg_hourly_output': round2(safe_div(o['produced'], max(o['entries'], 1)))
        }
        for o in by_operator
    ]

    by_equipment = entries.values('equipment_id', 'equipment__equipment_code', 'equipment__equipment_name').annotate(
        produced=Sum('quantity_produced'), rejected=Sum('quantity_rejected'), downtime=Sum('downtime_minutes'), entries=Count('id')
    ).order_by('-produced')[:10]

    top_equipment = [
        {
            'equipment_id': e['equipment_id'],
            'equipment_code': e['equipment__equipment_code'],
            'equipment_name': e['equipment__equipment_name'],
            'produced': float(e['produced'] or 0),
            'rejected': float(e['rejected'] or 0),
            'downtime_minutes': float(e['downtime'] or 0),
            'entries': int(e['entries']),
            'avg_hourly_output': round2(safe_div(e['produced'], max(e['entries'], 1)))
        }
        for e in by_equipment
    ]

    # Work order level summary
    wo_summary_qs = entries.values(
        'work_order_id', 
        'work_order__wo_number', 
        'work_order__product__sku',
        'work_order__product__product_name',
        'work_order__status'
    ).annotate(
        produced=Sum('quantity_produced'), 
        rejected=Sum('quantity_rejected'), 
        entries=Count('id')
    ).order_by('-produced')[:20]

    work_orders_summary = [
        {
            'work_order_id': w['work_order_id'],
            'wo_number': w['work_order__wo_number'],
            'product_sku': w['work_order__product__sku'],
            'product_name': w['work_order__product__product_name'],
            'status': w['work_order__status'],
            'produced': float(w['produced'] or 0),
            'rejected': float(w['rejected'] or 0),
            'entries': int(w['entries']),
            'yield_pct': round2(safe_div(w['produced'], (w['produced'] + w['rejected'])) * 100)
        }
        for w in wo_summary_qs
    ]

    res = {
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()},
        'total_entries': total_entries,
        'total_produced': float(total_produced),
        'total_rejected': float(total_rejected),
        'total_downtime_minutes': float(total_downtime),
        'distinct_work_orders': distinct_work_orders,
        'distinct_products': distinct_products,
        'distinct_equipment': distinct_equipment,
        'distinct_operators': distinct_operators,
        'yield_pct': round2(yield_pct),
        'avg_hourly_output': round2(avg_hourly_output),
        'top_operators': top_operators,
        'top_equipment': top_equipment,
        'work_orders_summary': work_orders_summary
    }

    cache.set(cache_key, res, 60)
    return Response(res)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def employee_production_detail(request, employee_id=None):
    """Detailed analytics for a single employee or all employees."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=400)

    base_filters = {'tenant': tenant, 'entry_datetime__date__range': [start_date, end_date]}
    
    if employee_id:
        employee = get_object_or_404(Employee, id=employee_id, tenant=tenant)
        base_filters['operator'] = employee

    entries = ProductionEntry.objects.filter(**base_filters).select_related('work_order', 'equipment', 'work_order__product')

    # Per-employee aggregation
    by_employee = entries.values('operator_id', 'operator__full_name', 'operator__employee_code', 'operator__department').annotate(
        produced=Sum('quantity_produced'), rejected=Sum('quantity_rejected'), downtime=Sum('downtime_minutes'), entries=Count('id')
    ).order_by('-produced')

    employees_data = []
    for e in by_employee:
        emp_entries_qs = entries.filter(operator_id=e['operator_id'])
        hours_worked = int(e['entries'])
        produced = float(e['produced'] or 0)
        rejected = float(e['rejected'] or 0)
        total_output = produced + rejected
        quality_rate = safe_div(produced, total_output) * 100
        avg_hourly = safe_div(produced, max(hours_worked, 1))

        # Breakdown by work order
        wo_breakdown = emp_entries_qs.values(
            'work_order_id', 
            'work_order__wo_number', 
            'work_order__product__sku',
            'work_order__product__product_name',
            'work_order__status',
            'work_order__quantity_planned'
        ).annotate(
            produced=Sum('quantity_produced'), 
            rejected=Sum('quantity_rejected'), 
            entries=Count('id')
        ).order_by('-produced')

        wo_list = [
            {
                'work_order_id': w['work_order_id'],
                'wo_number': w['work_order__wo_number'],
                'product_sku': w['work_order__product__sku'],
                'product_name': w['work_order__product__product_name'],
                'status': w['work_order__status'],
                'quantity_planned': float(w['work_order__quantity_planned'] or 0),
                'produced': float(w['produced'] or 0),
                'rejected': float(w['rejected'] or 0),
                'entries': int(w['entries']),
                'yield_pct': round2(safe_div(w['produced'], (w['produced'] + w['rejected'])) * 100)
            }
            for w in wo_breakdown
        ]

        # Recent production entries with full details
        recent_entries = emp_entries_qs.order_by('-entry_datetime')[:20]
        production_entries = [build_entry_dict(entry) for entry in recent_entries]

        employees_data.append({
            'employee_id': e['operator_id'],
            'employee_name': e['operator__full_name'],
            'employee_code': e['operator__employee_code'],
            'department': e['operator__department'],
            'hours_worked': hours_worked,
            'total_produced': round2(produced),
            'total_rejected': round2(rejected),
            'total_downtime_minutes': float(e['downtime'] or 0),
            'quality_rate_pct': round2(quality_rate),
            'avg_hourly_output': round2(avg_hourly),
            'work_order_breakdown': wo_list,
            'production_entries': production_entries
        })

    if employee_id and employees_data:
        return Response(employees_data[0])
    return Response({
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}, 
        'employees': employees_data
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def equipment_production_detail(request, equipment_id=None):
    """Detailed analytics per equipment or all equipment."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=400)

    base_filters = {'tenant': tenant, 'entry_datetime__date__range': [start_date, end_date]}
    if equipment_id:
        equipment = get_object_or_404(Equipment, id=equipment_id, tenant=tenant)
        base_filters['equipment'] = equipment

    entries = ProductionEntry.objects.filter(**base_filters).select_related('work_order', 'operator', 'work_order__product')

    # FIXED: Only include fields that exist in Equipment model
    by_equipment = entries.values(
        'equipment_id', 
        'equipment__equipment_code', 
        'equipment__equipment_name',
        'equipment__location'
    ).annotate(
        produced=Sum('quantity_produced'), 
        rejected=Sum('quantity_rejected'), 
        downtime=Sum('downtime_minutes'), 
        entries=Count('id')
    ).order_by('-produced')

    equipments = []
    for e in by_equipment:
        eq_entries_qs = entries.filter(equipment_id=e['equipment_id'])
        produced = float(e['produced'] or 0)
        rejected = float(e['rejected'] or 0)
        total_output = produced + rejected
        downtime = float(e['downtime'] or 0)
        entries_count = int(e['entries'])
        quality_rate = safe_div(produced, total_output) * 100

        # Work order breakdown for this equipment
        wo_breakdown = eq_entries_qs.values(
            'work_order_id',
            'work_order__wo_number',
            'work_order__product__sku',
            'work_order__product__product_name'
        ).annotate(
            produced=Sum('quantity_produced'),
            rejected=Sum('quantity_rejected'),
            entries=Count('id')
        ).order_by('-produced')

        wo_list = [
            {
                'work_order_id': w['work_order_id'],
                'wo_number': w['work_order__wo_number'],
                'product_sku': w['work_order__product__sku'],
                'product_name': w['work_order__product__product_name'],
                'produced': float(w['produced'] or 0),
                'rejected': float(w['rejected'] or 0),
                'entries': int(w['entries'])
            }
            for w in wo_breakdown
        ]

        # Recent production entries
        recent_entries = eq_entries_qs.order_by('-entry_datetime')[:20]
        production_entries = [build_entry_dict(entry) for entry in recent_entries]

        # FIXED: Only include existing fields
        equipments.append({
            'equipment_id': e['equipment_id'],
            'equipment_code': e['equipment__equipment_code'],
            'equipment_name': e['equipment__equipment_name'],
            'location': e['equipment__location'],
            'produced': round2(produced),
            'rejected': round2(rejected),
            'downtime_minutes': round2(downtime),
            'entries': entries_count,
            'quality_rate_pct': round2(quality_rate),
            'avg_hourly_output': round2(safe_div(produced, max(entries_count, 1))),
            'work_order_breakdown': wo_list,
            'production_entries': production_entries
        })

    if equipment_id and equipments:
        return Response(equipments[0])
    return Response({
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}, 
        'equipment': equipments
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def workorder_production_detail(request, workorder_id=None):
    """Detailed analytics for a work order with complete production data."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    start_date, end_date = parse_date_range(request, 90)
    if start_date is None:
        return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=400)

    base_filters = {'tenant': tenant, 'entry_datetime__date__range': [start_date, end_date]}
    if workorder_id:
        wo = get_object_or_404(WorkOrder, id=workorder_id, tenant=tenant)
        base_filters['work_order'] = wo

    entries = ProductionEntry.objects.filter(**base_filters).select_related('equipment', 'operator', 'work_order__product', 'work_order__cost_center')

    # FIXED: Remove 'work_order__product__description' as it doesn't exist in Product model
    by_wo = entries.values(
        'work_order_id', 
        'work_order__wo_number', 
        'work_order__product__sku',
        'work_order__product__product_name',
        'work_order__status',
        'work_order__quantity_planned',
        'work_order__quantity_completed',
        'work_order__quantity_scrapped',
        'work_order__due_date',
        'work_order__cost_center__name'
    ).annotate(
        produced=Sum('quantity_produced'), 
        rejected=Sum('quantity_rejected'), 
        downtime=Sum('downtime_minutes'), 
        entries=Count('id')
    ).order_by('-produced')

    wo_list = []
    for w in by_wo:
        entries_qs = entries.filter(work_order_id=w['work_order_id'])
        produced = float(w['produced'] or 0)
        rejected = float(w['rejected'] or 0)
        total_output = produced + rejected
        entries_count = int(w['entries'])
        quality_rate = safe_div(produced, total_output) * 100

        # Equipment contributions
        equip_contrib = entries_qs.values(
            'equipment_id', 
            'equipment__equipment_code',
            'equipment__equipment_name'
        ).annotate(
            produced=Sum('quantity_produced'), 
            rejected=Sum('quantity_rejected'), 
            downtime=Sum('downtime_minutes'), 
            entries=Count('id')
        ).order_by('-produced')

        equip_list = [
            {
                'equipment_id': e['equipment_id'],
                'equipment_code': e['equipment__equipment_code'],
                'equipment_name': e['equipment__equipment_name'],
                'produced': float(e['produced'] or 0),
                'rejected': float(e['rejected'] or 0),
                'downtime_minutes': float(e['downtime'] or 0),
                'entries': int(e['entries']),
                'share_pct': round2(safe_div(e['produced'], max(produced, 1)) * 100)
            }
            for e in equip_contrib
        ]

        # Operator contributions
        operator_contrib = entries_qs.values(
            'operator_id',
            'operator__full_name',
            'operator__employee_code',
            'operator__department'
        ).annotate(
            produced=Sum('quantity_produced'),
            rejected=Sum('quantity_rejected'),
            entries=Count('id')
        ).order_by('-produced')

        operator_list = [
            {
                'operator_id': op['operator_id'],
                'operator_name': op['operator__full_name'],
                'employee_code': op['operator__employee_code'],
                'department': op['operator__department'],
                'produced': float(op['produced'] or 0),
                'rejected': float(op['rejected'] or 0),
                'hours_worked': int(op['entries']),
                'share_pct': round2(safe_div(op['produced'], max(produced, 1)) * 100)
            }
            for op in operator_contrib
        ]

        # Production timeline
        timeline_entries = entries_qs.order_by('-entry_datetime')[:50]
        production_timeline = [build_entry_dict(entry) for entry in timeline_entries]

        # FIXED: Remove 'product_description' as it doesn't exist
        wo_list.append({
            'work_order_id': w['work_order_id'],
            'wo_number': w['work_order__wo_number'],
            'product_sku': w['work_order__product__sku'],
            'product_name': w['work_order__product__product_name'],
            'status': w['work_order__status'],
            'quantity_planned': float(w['work_order__quantity_planned'] or 0),
            'quantity_completed': float(w['work_order__quantity_completed'] or 0),
            'quantity_scrapped': float(w['work_order__quantity_scrapped'] or 0),
            'due_date': str(w['work_order__due_date']) if w['work_order__due_date'] else None,
            'cost_center': w['work_order__cost_center__name'],
            'produced': round2(produced),
            'rejected': round2(rejected),
            'entries': entries_count,
            'quality_rate_pct': round2(quality_rate),
            'avg_hourly_output': round2(safe_div(produced, max(entries_count, 1))),
            'equipment_contributions': equip_list,
            'operator_contributions': operator_list,
            'production_timeline': production_timeline
        })

    if workorder_id and wo_list:
        return Response(wo_list[0])
    return Response({
        'period': {'start_date': start_date.isoformat(), 'end_date': end_date.isoformat()}, 
        'work_orders': wo_list
    })


# ---------- Composite endpoint (All-in-one for a dashboard) ----------
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def production_dashboard(request):
    """One API call returning all key sections for a dashboard."""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    start_date, end_date = parse_date_range(request, 30)
    if start_date is None:
        return Response({'error': 'Invalid date format. Use YYYY-MM-DD'}, status=400)

    # Reuse existing functions
    summary = production_summary(request).data
    employees = employee_production_detail(request).data
    equipment = equipment_production_detail(request).data
    workorders = workorder_production_detail(request).data

    dashboard = {
        'summary': summary,
        'employees': employees,
        'equipment': equipment,
        'workorders': workorders
    }
    return Response(dashboard)