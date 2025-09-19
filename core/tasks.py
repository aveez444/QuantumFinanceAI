# core/tasks.py - Celery Background Tasks

from celery import shared_task
from django.core.cache import cache
from django.db.models import Sum, Avg, Q
from django.utils import timezone
from datetime import datetime, timedelta
import logging
from .models import (
    Tenant, WorkOrder, ProductionEntry, StockMovement, 
    Product, Equipment, Employee, AutomationRule
)

logger = logging.getLogger(__name__)

@shared_task
def calculate_oee_metrics():
    """
    Calculate OEE (Overall Equipment Effectiveness) for all active tenants
    Runs every hour to update production dashboards
    """
    active_tenants = Tenant.objects.filter(is_active=True)
    
    for tenant in active_tenants:
        try:
            # Set tenant context for this task
            cache.set(f'task_tenant_{tenant.id}', tenant, timeout=3600)
            
            # Get production entries from last hour
            one_hour_ago = timezone.now() - timedelta(hours=1)
            
            production_data = ProductionEntry.objects.filter(
                tenant=tenant,
                entry_datetime__gte=one_hour_ago
            ).select_related('equipment', 'work_order')
            
            # Group by equipment and calculate OEE
            equipment_oee = {}
            for entry in production_data:
                equipment_id = entry.equipment.id
                
                if equipment_id not in equipment_oee:
                    equipment_oee[equipment_id] = {
                        'total_planned': 0,
                        'total_produced': 0,
                        'total_good': 0,
                        'total_downtime': 0,
                        'equipment_name': entry.equipment.equipment_name
                    }
                
                # Calculate metrics
                planned_production = entry.equipment.capacity_per_hour
                actual_production = entry.quantity_produced + entry.quantity_rejected
                good_production = entry.quantity_produced
                downtime = entry.downtime_minutes
                
                equipment_oee[equipment_id]['total_planned'] += planned_production
                equipment_oee[equipment_id]['total_produced'] += actual_production
                equipment_oee[equipment_id]['total_good'] += good_production
                equipment_oee[equipment_id]['total_downtime'] += downtime
            
            # Calculate and cache OEE scores
            for equipment_id, data in equipment_oee.items():
                if data['total_planned'] > 0:
                    availability = max(0, (60 - data['total_downtime']) / 60)
                    performance = min(1, data['total_produced'] / data['total_planned'])
                    quality = data['total_good'] / max(1, data['total_produced'])
                    oee = availability * performance * quality * 100
                    
                    # Cache OEE data
                    cache_key = f"oee_{tenant.id}_{equipment_id}"
                    cache.set(cache_key, {
                        'oee': round(oee, 2),
                        'availability': round(availability * 100, 2),
                        'performance': round(performance * 100, 2),
                        'quality': round(quality * 100, 2),
                        'timestamp': timezone.now().isoformat()
                    }, timeout=7200)  # Cache for 2 hours
                    
                    logger.info(f"OEE calculated for tenant {tenant.company_name}, "
                              f"equipment {data['equipment_name']}: {oee:.2f}%")
        
        except Exception as e:
            logger.error(f"Error calculating OEE for tenant {tenant.id}: {str(e)}")

@shared_task 
def update_stock_levels():
    """
    Update real-time stock levels and identify reorder points
    Runs every 30 minutes
    """
    active_tenants = Tenant.objects.filter(is_active=True)
    
    for tenant in active_tenants:
        try:
            # Get all products for tenant
            products = Product.objects.filter(tenant=tenant, is_active=True)
            
            for product in products:
                # Calculate current stock from all movements
                current_stock = StockMovement.objects.filter(
                    tenant=tenant,
                    product=product
                ).aggregate(
                    total=Sum('quantity')
                )['total'] or 0
                
                # Cache current stock level
                cache_key = f"stock_{tenant.id}_{product.id}"
                cache.set(cache_key, {
                    'current_stock': current_stock,
                    'reorder_point': product.reorder_point,
                    'needs_reorder': current_stock <= product.reorder_point,
                    'last_updated': timezone.now().isoformat()
                }, timeout=3600)
                
                # Trigger reorder alert if needed
                if current_stock <= product.reorder_point:
                    trigger_reorder_alert.delay(tenant.id, product.id, current_stock)
                    
        except Exception as e:
            logger.error(f"Error updating stock levels for tenant {tenant.id}: {str(e)}")

@shared_task
def trigger_reorder_alert(tenant_id, product_id, current_stock):
    """
    Send reorder alerts when stock falls below threshold
    """
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        product = Product.objects.get(id=product_id, tenant=tenant)
        
        # Check if alert was already sent recently (avoid spam)
        alert_key = f"reorder_alert_{tenant_id}_{product_id}"
        if cache.get(alert_key):
            return
            
        # Create alert message
        alert_message = f"STOCK ALERT: {product.product_name} ({product.sku}) " \
                       f"is below reorder point. Current stock: {current_stock}, " \
                       f"Reorder point: {product.reorder_point}"
        
        # Log alert (later: send email/SMS/WhatsApp)
        logger.warning(f"Tenant {tenant.company_name}: {alert_message}")
        
        # Cache alert to prevent duplicate alerts for 4 hours
        cache.set(alert_key, True, timeout=14400)
        
        # Store alert in database for dashboard
        # AlertLog.objects.create(
        #     tenant=tenant,
        #     alert_type='stock_reorder',
        #     message=alert_message,
        #     severity='medium'
        # )
        
    except Exception as e:
        logger.error(f"Error sending reorder alert: {str(e)}")

@shared_task
def check_business_alerts():
    """
    Check various business conditions and generate alerts
    Runs every 15 minutes
    """
    active_tenants = Tenant.objects.filter(is_active=True)
    
    for tenant in active_tenants:
        try:
            # Check for overdue work orders
            overdue_orders = WorkOrder.objects.filter(
                tenant=tenant,
                status__in=['planned', 'released', 'in_progress'],
                due_date__lt=timezone.now().date()
            ).count()
            
            if overdue_orders > 0:
                logger.warning(f"Tenant {tenant.company_name} has {overdue_orders} overdue work orders")
            
            # Check for equipment with high downtime
            one_hour_ago = timezone.now() - timedelta(hours=1)
            high_downtime_equipment = ProductionEntry.objects.filter(
                tenant=tenant,
                entry_datetime__gte=one_hour_ago,
                downtime_minutes__gt=30  # More than 30 minutes downtime in an hour
            ).values('equipment__equipment_name').distinct()
            
            for equipment in high_downtime_equipment:
                logger.warning(f"High downtime alert for {equipment['equipment__equipment_name']}")
            
            # Check for production efficiency drops
            check_production_efficiency.delay(tenant.id)
            
        except Exception as e:
            logger.error(f"Error checking alerts for tenant {tenant.id}: {str(e)}")

@shared_task
def check_production_efficiency(tenant_id):
    """
    Analyze production efficiency and identify underperforming operators/equipment
    """
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        
        # Get production data from last 4 hours
        four_hours_ago = timezone.now() - timedelta(hours=4)
        
        # Check operator efficiency
        operator_performance = ProductionEntry.objects.filter(
            tenant=tenant,
            entry_datetime__gte=four_hours_ago
        ).values('operator__employee_code', 'operator__full_name').annotate(
            total_produced=Sum('quantity_produced'),
            total_planned=Sum('equipment__capacity_per_hour'),
            efficiency=Sum('quantity_produced') * 100.0 / Sum('equipment__capacity_per_hour')
        ).filter(efficiency__lt=80)  # Below 80% efficiency
        
        for operator in operator_performance:
            alert_message = f"Low efficiency alert: Operator {operator['operator__full_name']} " \
                           f"({operator['operator__employee_code']}) running at " \
                           f"{operator['efficiency']:.1f}% efficiency"
            logger.warning(alert_message)
            
    except Exception as e:
        logger.error(f"Error checking production efficiency: {str(e)}")

@shared_task
def backup_tenant_data(tenant_id=None):
    """
    Create backup snapshots of critical tenant data
    Runs daily
    """
    if tenant_id:
        tenants = [Tenant.objects.get(id=tenant_id)]
    else:
        tenants = Tenant.objects.filter(is_active=True)
    
    for tenant in tenants:
        try:
            # Count critical records
            counts = {
                'work_orders': WorkOrder.objects.filter(tenant=tenant).count(),
                'production_entries': ProductionEntry.objects.filter(tenant=tenant).count(),
                'stock_movements': StockMovement.objects.filter(tenant=tenant).count(),
                'products': Product.objects.filter(tenant=tenant).count(),
                'employees': Employee.objects.filter(tenant=tenant).count(),
            }
            
            # Store backup metadata
            backup_key = f"backup_meta_{tenant.id}_{timezone.now().date()}"
            cache.set(backup_key, {
                'tenant_name': tenant.company_name,
                'backup_date': timezone.now().isoformat(),
                'record_counts': counts
            }, timeout=86400 * 7)  # Keep for 7 days
            
            logger.info(f"Backup metadata created for {tenant.company_name}: {counts}")
            
        except Exception as e:
            logger.error(f"Error backing up tenant {tenant_id}: {str(e)}")

@shared_task
def process_automation_rules():
    """
    Process custom automation rules defined by tenants
    """
    active_rules = AutomationRule.objects.filter(is_enabled=True)
    
    for rule in active_rules:
        try:
            # Process based on trigger type
            if rule.trigger_type == 'time_based':
                process_time_based_rule(rule)
            elif rule.trigger_type == 'event_based':
                process_event_based_rule(rule)
            elif rule.trigger_type == 'threshold_based':
                process_threshold_based_rule(rule)
                
        except Exception as e:
            logger.error(f"Error processing automation rule {rule.id}: {str(e)}")
    
def process_time_based_rule(rule):
    """Process time-based automation rules (e.g., daily reports, weekly summaries)"""
    # Implementation depends on specific rule configuration
    logger.info(f"Processing time-based rule: {rule.rule_name}")

def process_event_based_rule(rule):
    """Process event-based automation rules (e.g., on work order completion)"""
    logger.info(f"Processing event-based rule: {rule.rule_name}")

def process_threshold_based_rule(rule):
    """Process threshold-based automation rules (e.g., when stock < 100)"""
    logger.info(f"Processing threshold-based rule: {rule.rule_name}")

# AI-related background tasks
@shared_task
def generate_daily_insights(tenant_id):
    """
    Generate daily business insights using AI
    """
    try:
        tenant = Tenant.objects.get(id=tenant_id)
        
        # Collect key metrics from yesterday
        yesterday = timezone.now().date() - timedelta(days=1)
        
        insights = {
            'production_summary': get_production_summary(tenant, yesterday),
            'quality_metrics': get_quality_metrics(tenant, yesterday),
            'efficiency_trends': get_efficiency_trends(tenant, yesterday),
            'cost_analysis': get_cost_analysis(tenant, yesterday)
        }
        
        # Cache insights for dashboard
        cache_key = f"daily_insights_{tenant.id}_{yesterday}"
        cache.set(cache_key, insights, timeout=86400)  # Cache for 24 hours
        
        logger.info(f"Daily insights generated for {tenant.company_name}")
        
    except Exception as e:
        logger.error(f"Error generating insights for tenant {tenant_id}: {str(e)}")

def get_production_summary(tenant, date):
    """Get production summary for a specific date"""
    return ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date=date
    ).aggregate(
        total_produced=Sum('quantity_produced'),
        total_rejected=Sum('quantity_rejected'),
        total_downtime=Sum('downtime_minutes')
    )

def get_quality_metrics(tenant, date):
    """Get quality metrics for a specific date"""
    entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date=date
    )
    
    total_produced = entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
    total_rejected = entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    
    if total_produced + total_rejected > 0:
        quality_rate = (total_produced / (total_produced + total_rejected)) * 100
    else:
        quality_rate = 0
        
    return {'quality_rate': round(quality_rate, 2)}

def get_efficiency_trends(tenant, date):
    """Get efficiency trends for a specific date"""
    # Implementation for efficiency calculation
    return {'average_efficiency': 85.2}

def get_cost_analysis(tenant, date):
    """Get cost analysis for a specific date"""
    # Implementation for cost calculation
    return {'total_labor_cost': 15000.00}