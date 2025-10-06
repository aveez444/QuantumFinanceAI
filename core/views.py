from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import api_view, action, permission_classes
from rest_framework import status, permissions, generics, viewsets, filters
from django.contrib.auth import login, logout  
from rest_framework.permissions import IsAuthenticated, AllowAny
from .serializers import TenantWithAdminSerializer, WarehouseSerializer, EmployeeDocumentSerializer, PaymentAdviceSerializer, CustomerPurchaseOrderSerializer, CustomerInvoiceSerializer, PurchaseOrderSerializer, ChartOfAccountsSerializer, LoginSerializer, ProductSerializer, WorkOrderSerializer, ProductionEntrySerializer, EquipmentSerializer, EmployeeSerializer, StockMovementSerializer, GLJournalSerializer, CostCenterSerializer, PartySerializer
from django.core.cache import cache
from django.db.models import Sum, Avg, Count, Q, F, Case, When, DecimalField, Max
from typing import Dict, List, Any, Optional
from django.db import transaction
from django.utils import timezone
from django.shortcuts import get_object_or_404
from datetime import datetime, timedelta
from decimal import Decimal
from django.middleware.csrf import get_token
import os  
from rest_framework import parsers
from django.http import HttpResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer
from reportlab.lib import colors
import io

from .reconciliation_service import ReconciliationService
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from .models import AIQueryLog
import json
from django.views.decorators.csrf import csrf_exempt
import time  # For execution timing
import logging
from .middleware import get_current_tenant
from .utils import calculate_oee, generate_movement_number, create_automated_gl_entry
from django.conf import settings
from .llm_utils import call_llm
# Add this import at the top of views.py
from .enhanced_ai_engine import ERPAIEngine


from .models import (
    Tenant, TenantUser, Product, WorkOrder, ProductionEntry, CustomerInvoice, 
    PaymentAdvice, 
    PaymentAdviceInvoice, CustomerPurchaseOrder,
    Equipment, Employee, StockMovement, ChartOfAccounts, 
    GLJournal, GLJournalLine, CostCenter, Warehouse, Party, PurchaseOrder
)


class CreateTenantView(APIView):
    """Superuser-only: create a tenant + first admin user"""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, *args, **kwargs):
        serializer = TenantWithAdminSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant, user = serializer.save()

        # Optional: call setup_default_data.delay(tenant.id) here

        return Response({
            "message": "Tenant and admin created successfully",
            "tenant": {
                "id": tenant.id,
                "company_name": tenant.company_name,
                "subdomain": tenant.subdomain,
                "plan_type": tenant.plan_type,
            },
            "admin_user": {
                "id": user.id,
                "username": user.username,
                "email": user.email,
            }
        }, status=status.HTTP_201_CREATED)

class GetCSRFTokenView(APIView):
    """Return CSRF token"""
    permission_classes = [AllowAny]  # 🔑 public endpoint

    def get(self, request, *args, **kwargs):
        csrf_token = get_token(request)
        return Response({"csrfToken": csrf_token})

class LoginView(APIView):
    """Login using JWT"""
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        
        return Response({
            "message": "Logged in successfully",
            "access": str(refresh.access_token),
            "refresh": str(refresh)
        })
# core/views.py - Add blacklist endpoint
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

class LogoutView(APIView):
    """Logout - blacklist refresh token"""
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        try:
            refresh_token = request.data.get("refresh")
            if refresh_token:
                token = RefreshToken(refresh_token)
                token.blacklist()
            
            return Response({"message": "Logged out successfully"})
        except TokenError:
            return Response({"error": "Invalid token"}, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

logger = logging.getLogger(__name__)

def setup_default_master_data(tenant):
    """Initialize tenant with default master data"""
    # Default cost centers
    cost_centers = [
        {'cost_center_code': 'PROD-001', 'name': 'Production Floor'},
        {'cost_center_code': 'QC-001', 'name': 'Quality Control'},
        {'cost_center_code': 'WH-001', 'name': 'Warehouse'},
        {'cost_center_code': 'ADMIN-001', 'name': 'Administration'}
    ]
    
    for cc_data in cost_centers:
        CostCenter.objects.create(tenant=tenant, **cc_data)
    
    # Default chart of accounts
    accounts = [
        {'account_code': '1000', 'account_name': 'Cash', 'account_type': 'asset'},
        {'account_code': '1200', 'account_name': 'Accounts Receivable', 'account_type': 'asset'},
        {'account_code': '1300', 'account_name': 'Inventory', 'account_type': 'asset'},
        {'account_code': '2000', 'account_name': 'Accounts Payable', 'account_type': 'liability'},
        {'account_code': '4000', 'account_name': 'Sales Revenue', 'account_type': 'revenue'},
        {'account_code': '5000', 'account_name': 'Cost of Goods Sold', 'account_type': 'cogs'},
        {'account_code': '6000', 'account_name': 'Operating Expenses', 'account_type': 'expense'}
    ]
    
    for acc_data in accounts:
        ChartOfAccounts.objects.create(tenant=tenant, **acc_data)
    
    # Default warehouse
    Warehouse.objects.create(
        tenant=tenant,
        warehouse_code='WH-MAIN',
        warehouse_name='Main Warehouse',
        location='Default Location'
    )

@api_view(['GET'])
def tenant_info(request):
    """Get current tenant context and capabilities"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    return Response({
        'tenant_id': tenant.id,
        'company_name': tenant.company_name,
        'plan_type': tenant.plan_type,
        'modules_enabled': tenant.modules_enabled,
        'created_at': tenant.created_at,
    })

# ===== MASTER DATA MANAGEMENT =====

class ProductViewSet(viewsets.ModelViewSet):
    """Product master with stock integration"""
    serializer_class = ProductSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['sku', 'product_name', 'category']
    ordering_fields = ['sku', 'product_name', 'created_at']
    ordering = ['sku']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return Product.objects.filter(tenant=tenant, is_active=True) if tenant else Product.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)
    
    @action(detail=False, methods=['get'])
    def stock_overview(self, request):
        """Current stock levels with reorder alerts"""
        tenant = get_current_tenant()
        products = self.get_queryset()
        
        stock_data = []
        for product in products:
            # Calculate current stock
            current_stock = StockMovement.objects.filter(
                tenant=tenant, product=product
            ).aggregate(total=Sum('quantity'))['total'] or 0
            
            # Check recent movements
            recent_movements = StockMovement.objects.filter(
                tenant=tenant, product=product,
                movement_date__gte=timezone.now() - timedelta(days=30)
            ).count()
            
            stock_data.append({
                'product_id': product.id,
                'sku': product.sku,
                'product_name': product.product_name,
                'current_stock': float(current_stock),
                'reorder_point': product.reorder_point,
                'needs_reorder': current_stock <= product.reorder_point,
                'recent_activity': recent_movements > 0,
                'standard_cost': float(product.standard_cost)
            })
        
        return Response({
            'products': stock_data,
            'total_products': len(stock_data),
            'reorder_needed': sum(1 for p in stock_data if p['needs_reorder'])
        })

    @action(detail=False, methods=['get'], url_path='stock-report')
    def stock_report(self, request):
        tenant = get_current_tenant()
        products = Product.objects.filter(tenant=tenant, is_active=True)
        
        data = []
        for p in products:
            # Calculate current stock
            movements = StockMovement.objects.filter(tenant=tenant, product=p)
            current_stock = movements.aggregate(total=Sum('quantity'))['total'] or 0
            
            # Warehouse breakdown
            warehouse_stocks = movements.values('warehouse__warehouse_name').annotate(stock=Sum('quantity')).order_by('warehouse__warehouse_name')
            breakdown = [
                {'warehouse': ws['warehouse__warehouse_name'], 'stock': float(ws['stock'] or 0)}
                for ws in warehouse_stocks if ws['stock'] != 0
            ]
            
            # Last movement
            last_movement = movements.order_by('-movement_date').first()
            
            data.append({
                'sku': p.sku,
                'product_name': p.product_name,
                'current_stock': float(current_stock),
                'reorder_point': p.reorder_point,
                'standard_cost': float(p.standard_cost),
                'stock_value': float(current_stock * p.standard_cost),
                'last_movement_date': last_movement.movement_date if last_movement else None,
                'warehouse_breakdown': breakdown
            })
        
        return Response({
            'report_date': timezone.now().date(),
            'total_value': sum(d['stock_value'] for d in data),
            'total_items': len(data),
            'data': data
        })
    @action(detail=True, methods=['post'], parser_classes=[parsers.MultiPartParser])
    def upload_image(self, request, pk=None):
            """Upload primary product image"""
            product = self.get_object()
            
            if 'image' not in request.FILES:
                return Response({'error': 'No image file provided'}, status=400)
            
            # Delete old image if exists
            if product.primary_image:
                product.primary_image.delete(save=False)
            
            product.primary_image = request.FILES['image']
            product.save()
            
            return Response({
                'message': 'Image uploaded successfully',
                'image_url': product.primary_image.url if product.primary_image else None
            })

    @action(detail=True, methods=['delete'])
    def delete_image(self, request, pk=None):
            """Delete primary product image"""
            product = self.get_object()
            
            if product.primary_image:
                product.primary_image.delete(save=True)
                return Response({'message': 'Image deleted successfully'})
            
            return Response({'error': 'No image to delete'}, status=400)
            
    @action(detail=False, methods=['get'], url_path='stock-report-pdf')
    def stock_report_pdf(self, request):
        tenant = get_current_tenant()
        products = Product.objects.filter(tenant=tenant, is_active=True)
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        
        # Header
        elements.append(Paragraph(tenant.company_name, styles['Heading1']))
        elements.append(Paragraph("Stock Report", styles['Heading2']))
        elements.append(Paragraph(f"Generated on: {timezone.now().date()}", styles['Normal']))
        elements.append(Paragraph("", styles['Normal']))  # Spacer
        
        # Table data
        data = [['SKU', 'Product Name', 'Stock', 'Value', 'Reorder Point']]
        total_value = 0
        for p in products:
            current_stock = StockMovement.objects.filter(tenant=tenant, product=p).aggregate(total=Sum('quantity'))['total'] or 0
            value = current_stock * p.standard_cost
            total_value += value
            data.append([
                p.sku,
                p.product_name,
                f"{current_stock}",
                f"${value:.2f}",
                str(p.reorder_point)
            ])
        
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black)
        ]))
        elements.append(table)
        
        # Footer
        elements.append(Paragraph("", styles['Normal']))  # Spacer
        elements.append(Paragraph(f"Total Stock Value: ${total_value:.2f}", styles['Heading3']))
        
        doc.build(elements)
        buffer.seek(0)
        
        response = HttpResponse(buffer, content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="stock_report.pdf"'
        return response

class PartyViewSet(viewsets.ModelViewSet):
    """Customer/Supplier master data"""
    serializer_class = PartySerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['party_code', 'legal_name', 'display_name']
    ordering = ['party_code']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        queryset = Party.objects.filter(tenant=tenant, is_active=True) if tenant else Party.objects.none()
        
        # Filter by party type if specified
        party_type = self.request.query_params.get('party_type')
        if party_type:
            queryset = queryset.filter(party_type=party_type)
        
        return queryset
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)

# ===== PRODUCTION MANAGEMENT =====

class WorkOrderViewSet(viewsets.ModelViewSet):
    """Work Order lifecycle management"""
    serializer_class = WorkOrderSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['wo_number', 'product__sku', 'product__product_name']
    ordering_fields = ['wo_number', 'due_date', 'priority', 'created_at']
    ordering = ['-created_at']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        queryset = WorkOrder.objects.filter(tenant=tenant, is_active=True) if tenant else WorkOrder.objects.none()
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        return queryset.select_related('product', 'cost_center')
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        # Auto-generate WO number
        last_wo = WorkOrder.objects.filter(tenant=tenant).order_by('-id').first()
        wo_number = f"WO-{timezone.now().strftime('%Y%m')}-{(last_wo.id + 1) if last_wo else 1:04d}"
        serializer.save(tenant=tenant, created_by=self.request.user, wo_number=wo_number)
    
    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        """Authorize / release a planned work order so it can accept production entries"""
        work_order = self.get_object()
        
        if work_order.status != 'planned':
            return Response({'error': 'Only planned work orders can be released'}, status=400)
        
        work_order.status = 'released'
        work_order.save()
        
        logger.info(f"Work Order {work_order.wo_number} released by {request.user.username}")
        return Response({'message': 'Work order released', 'status': work_order.status})
    
    @action(detail=True, methods=['post'])
    def start_production(self, request, pk=None):
        """Start work order production (must be released first)"""
        work_order = self.get_object()
        
        if work_order.status != 'released':
            return Response({'error': 'Can only start work orders that are released'}, status=400)
        
        work_order.status = 'in_progress'
        work_order.save()
        
        logger.info(f"Work Order {work_order.wo_number} started by {request.user.username}")
        
        return Response({'message': 'Work order started', 'status': work_order.status})
        
    @action(detail=True, methods=['post'])
    def complete_production(self, request, pk=None):
        """Complete work order with final counts"""
        work_order = self.get_object()
        
        if work_order.status != 'in_progress':
            return Response({'error': 'Work order must be in progress'}, status=400)
        
        # Get final production data
        final_qty = request.data.get('final_quantity_completed')
        if final_qty is not None:
            # If coming as string, try to coerce to int/float as appropriate
            try:
                work_order.quantity_completed = float(final_qty)
            except Exception:
                work_order.quantity_completed = final_qty
        
        work_order.status = 'completed'
        work_order.save()

        if work_order.tenant.modules_enabled.get('finance'):
            try:
                journal_numbers = create_automated_gl_entry(
                    work_order.tenant,
                    'production_completion',
                    {'work_order_id': work_order.id},
                    user=request.user
                )
                logger.info(f"GL entries created: {journal_numbers}")
            except Exception as e:
                logger.warning(f"GL automation failed: {e}")
        
        # Auto-create stock receipt for finished goods
        self.create_production_receipt(work_order, request.user)  # Pass user
        
        return Response({
            'message': 'Work order completed',
            'completion_percentage': work_order.completion_percentage
        })

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Simple cancel: only sets status='cancelled'.
        Allowed from: planned, released, in_progress
        Not allowed from: completed (returns 400)
        """
        work_order = self.get_object()

        if work_order.status == 'completed':
            return Response({'error': 'Completed work orders cannot be cancelled'}, status=status.HTTP_400_BAD_REQUEST)

        if work_order.status == 'cancelled':
            return Response({'message': 'Work order already cancelled', 'status': work_order.status}, status=status.HTTP_200_OK)

        # Optionally enforce role/permission here:
        # if not request.user.has_perm('core.cancel_workorder'):
        #     return Response({'error': 'Permission denied'}, status=status.HTTP_403_FORBIDDEN)

        work_order.status = 'cancelled'
        work_order.save(update_fields=['status'])

        # optional: log the event
        logger.info(f"Work Order {work_order.wo_number} cancelled by {request.user.username}")

        return Response({'message': 'Work order cancelled', 'status': work_order.status}, status=status.HTTP_200_OK)
        
    def create_production_receipt(self, work_order, user):
        """Create stock movement for completed production"""
        if work_order.quantity_completed > 0:
            # Get default warehouse
            warehouse = Warehouse.objects.filter(tenant=work_order.tenant).first()
            if warehouse:
                StockMovement.objects.create(
                    tenant=work_order.tenant,  # Use work_order.tenant
                    movement_number=generate_movement_number(work_order.tenant, 'PROD'),
                    movement_type='production_receipt',
                    product=work_order.product,
                    warehouse=warehouse,
                    quantity=work_order.quantity_completed,
                    unit_cost=work_order.product.standard_cost,
                    reference_doc=work_order.wo_number,
                    movement_date=timezone.now(),
                    created_by=user  # Use passed user parameter
                )
    
    @action(detail=False, methods=['get'])
    def dashboard_summary(self, request):
        """Production dashboard KPIs"""
        tenant = get_current_tenant()
        today = timezone.now().date()
        
        work_orders = self.get_queryset()
        
        summary = {
            'total_orders': work_orders.count(),
            'in_progress': work_orders.filter(status='in_progress').count(),
            'completed_today': work_orders.filter(
                status='completed',
                updated_at__date=today
            ).count(),
            'overdue': work_orders.filter(
                due_date__lt=today,
                status__in=['planned', 'in_progress']
            ).count(),
            'total_planned_qty': work_orders.aggregate(Sum('quantity_planned'))['quantity_planned__sum'] or 0,
            'total_completed_qty': work_orders.aggregate(Sum('quantity_completed'))['quantity_completed__sum'] or 0
        }
        
        # Calculate average completion rate
        completed_orders = work_orders.exclude(quantity_planned=0)
        if completed_orders.exists():
            summary['avg_completion_rate'] = completed_orders.aggregate(
                avg_rate=Avg(F('quantity_completed') * 100.0 / F('quantity_planned'))
            )['avg_rate'] or 0
        else:
            summary['avg_completion_rate'] = 0
        
        return Response(summary)

class ProductionEntryViewSet(viewsets.ModelViewSet):
    """Hourly production recording with OEE calculation"""
    serializer_class = ProductionEntrySerializer
    permission_classes = [IsAuthenticated]
    ordering = ['-entry_datetime']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        queryset = ProductionEntry.objects.filter(tenant=tenant) if tenant else ProductionEntry.objects.none()
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        
        if start_date:
            queryset = queryset.filter(entry_datetime__gte=start_date)
        if end_date:
            queryset = queryset.filter(entry_datetime__lte=end_date)
        
        return queryset.select_related('work_order', 'equipment', 'operator')
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        entry = serializer.save(tenant=tenant, created_by=self.request.user)
        
        # Update work order progress
        self.update_work_order_progress(entry)
        
        # Clear relevant caches
        cache.delete(f"oee_{tenant.id}_{entry.equipment.id}")
    
    def update_work_order_progress(self, entry):
        """Update work order with latest production figures"""
        work_order = entry.work_order
        
        # Aggregate all production for this work order
        total_produced = ProductionEntry.objects.filter(
            work_order=work_order
        ).aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
        
        total_rejected = ProductionEntry.objects.filter(
            work_order=work_order
        ).aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
        
        work_order.quantity_completed = total_produced
        work_order.quantity_scrapped = total_rejected
        work_order.save()
    
    @action(detail=False, methods=['post'])
    def bulk_entry(self, request):
        """Bulk production entry for shift handover"""
        tenant = get_current_tenant()
        entries_data = request.data.get('entries', [])
        
        if not entries_data:
            return Response({'error': 'No entries provided'}, status=400)
        
        created_entries = []
        errors = []
        
        with transaction.atomic():
            for i, entry_data in enumerate(entries_data):
                try:
                    entry_data['tenant'] = tenant.id
                    serializer = self.get_serializer(data=entry_data)
                    if serializer.is_valid():
                        entry = serializer.save(created_by=request.user)
                        created_entries.append(entry.id)
                        self.update_work_order_progress(entry)
                    else:
                        errors.append(f"Entry {i+1}: {serializer.errors}")
                except Exception as e:
                    errors.append(f"Entry {i+1}: {str(e)}")
        
        return Response({
            'created_count': len(created_entries),
            'errors': errors,
            'entry_ids': created_entries
        }, status=201 if created_entries else 400)
    
    @action(detail=False, methods=['get'])
    def oee_metrics(self, request):
        """Calculate OEE metrics for equipment"""
        tenant = get_current_tenant()
        equipment_id = request.query_params.get('equipment_id')
        date_filter = request.query_params.get('date', timezone.now().date())
        
        if equipment_id:
            equipment_list = [get_object_or_404(Equipment, id=equipment_id, tenant=tenant)]
        else:
            equipment_list = Equipment.objects.filter(tenant=tenant, is_active=True)
        
        oee_data = []
        for equipment in equipment_list:
            oee_metrics = calculate_oee(equipment, date_filter)
            oee_data.append({
                'equipment_id': equipment.id,
                'equipment_name': equipment.equipment_name,
                'date': date_filter,
                **oee_metrics
            })
        
        return Response(oee_data)

# ===== INVENTORY MANAGEMENT =====

class StockMovementViewSet(viewsets.ModelViewSet):
    """Inventory movements with real-time stock tracking"""
    serializer_class = StockMovementSerializer
    permission_classes = [IsAuthenticated]
    ordering = ['-movement_date']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        queryset = StockMovement.objects.filter(tenant=tenant) if tenant else StockMovement.objects.none()
        
        # Filter by movement type
        movement_type = self.request.query_params.get('movement_type')
        if movement_type:
            queryset = queryset.filter(movement_type=movement_type)
        
        # Filter by product
        product_id = self.request.query_params.get('product_id')
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        
        return queryset.select_related('product', 'warehouse')
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        
        # Auto-generate movement number
        movement_number = generate_movement_number(tenant, serializer.validated_data['movement_type'])
        
        movement = serializer.save(
            tenant=tenant,
            created_by=self.request.user,
            movement_number=movement_number
        )
        
        # Clear stock cache for this product
        cache.delete(f"stock_{tenant.id}_{movement.product.id}")
        
        # Create GL entries for inventory valuation
        self.create_inventory_gl_entries(movement)
    
    def create_inventory_gl_entries(self, movement):
        """Create GL entries for inventory movements"""
        if movement.movement_type in ['receipt', 'production_receipt']:
            # Debit Inventory, Credit varies by source
            value = movement.quantity * movement.unit_cost
            
            # Find inventory account
            inventory_account = ChartOfAccounts.objects.filter(
                tenant=movement.tenant,
                account_type='asset',
                account_name__icontains='inventory'
            ).first()
            
            if inventory_account and value > 0:
                # Create journal entry
                journal = GLJournal.objects.create(
                    tenant=movement.tenant,
                    journal_number=f"INV-{movement.movement_number}",
                    posting_date=movement.movement_date.date(),
                    reference=f"Stock Movement {movement.movement_number}",
                    total_debit=value,
                    total_credit=value,
                    status='posted',
                    created_by=self.request.user
                )
                
                # Debit inventory
                GLJournalLine.objects.create(
                    tenant=movement.tenant,
                    journal=journal,
                    line_number=1,
                    account=inventory_account,
                    debit_amount=value,
                    description=f"Stock receipt {movement.product.sku}",
                    created_by=self.request.user
                )
    
        # In StockMovementViewSet class in views.py
    def warehouse_stock(self, request):
        """Get current stock levels for a specific warehouse"""
        tenant = get_current_tenant()
        warehouse_id = request.query_params.get('warehouse_id')
        
        if not warehouse_id:
            return Response({'error': 'warehouse_id parameter is required'}, status=400)
        
        try:
            warehouse = Warehouse.objects.get(id=warehouse_id, tenant=tenant)
        except Warehouse.DoesNotExist:
            return Response({'error': 'Warehouse not found'}, status=404)
        
        # Aggregate stock by product for the specific warehouse
        stock_summary = StockMovement.objects.filter(
            tenant=tenant,
            warehouse=warehouse
        ).values(
            'product__sku',
            'product__product_name',
            'product__uom'
        ).annotate(
            current_stock=Sum('quantity'),
            last_movement=Max('movement_date')
        ).order_by('product__sku')
        
        return Response(list(stock_summary))

    @action(detail=False, methods=['get'])
    def current_stock(self, request):
        """Get current stock levels by warehouse"""
        tenant = get_current_tenant()
        
        # Aggregate stock by product and warehouse
        stock_summary = StockMovement.objects.filter(
            tenant=tenant
        ).values(
            'product__sku',
            'product__product_name',
            'warehouse__warehouse_name'
        ).annotate(
            current_stock=Sum('quantity'),
            last_movement=Max('movement_date')
        ).order_by('product__sku', 'warehouse__warehouse_name')
        
        return Response(list(stock_summary))
    
    @action(detail=False, methods=['post'])
    def stock_transfer(self, request):
        """Transfer stock between warehouses"""
        tenant = get_current_tenant()
        data = request.data
        
        required_fields = ['product_id', 'from_warehouse_id', 'to_warehouse_id', 'quantity']
        if not all(field in data for field in required_fields):
            return Response({'error': 'Missing required fields'}, status=400)
        
        try:
            with transaction.atomic():
                product = get_object_or_404(Product, id=data['product_id'], tenant=tenant)
                from_warehouse = get_object_or_404(Warehouse, id=data['from_warehouse_id'], tenant=tenant)
                to_warehouse = get_object_or_404(Warehouse, id=data['to_warehouse_id'], tenant=tenant)
                
                quantity = Decimal(str(data['quantity']))
                
                # Check available stock
                available_stock = StockMovement.objects.filter(
                    tenant=tenant,
                    product=product,
                    warehouse=from_warehouse
                ).aggregate(Sum('quantity'))['quantity__sum'] or 0
                
                if available_stock < quantity:
                    return Response({'error': 'Insufficient stock for transfer'}, status=400)
                
                # Create transfer out
                transfer_number = generate_movement_number(tenant, 'TRANSFER')
                
                StockMovement.objects.create(
                    tenant=tenant,
                    movement_number=f"{transfer_number}-OUT",
                    movement_type='transfer_out',
                    product=product,
                    warehouse=from_warehouse,
                    quantity=-quantity,
                    unit_cost=product.standard_cost,
                    reference_doc=transfer_number,
                    movement_date=timezone.now(),
                    created_by=request.user
                )
                
                # Create transfer in
                StockMovement.objects.create(
                    tenant=tenant,
                    movement_number=f"{transfer_number}-IN",
                    movement_type='transfer_in',
                    product=product,
                    warehouse=to_warehouse,
                    quantity=quantity,
                    unit_cost=product.standard_cost,
                    reference_doc=transfer_number,
                    movement_date=timezone.now(),
                    created_by=request.user
                )
                
                # Clear cache
                cache.delete(f"stock_{tenant.id}_{product.id}")
                
                return Response({'message': 'Stock transfer completed', 'transfer_number': transfer_number})
                
        except Exception as e:
            logger.error(f"Stock transfer failed: {str(e)}")
            return Response({'error': 'Transfer failed'}, status=500)

class EquipmentViewSet(viewsets.ModelViewSet):
    """Equipment master with maintenance tracking"""
    serializer_class = EquipmentSerializer
    permission_classes = [IsAuthenticated]
    ordering = ['equipment_code']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return Equipment.objects.filter(tenant=tenant, is_active=True) if tenant else Equipment.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)
    
    @action(detail=False, methods=['get'])
    def maintenance_schedule(self, request):
        """Get equipment maintenance schedule"""
        equipment_list = self.get_queryset()
        
        maintenance_data = []
        for equipment in equipment_list:
            next_maintenance = equipment.next_maintenance
            overdue = next_maintenance and next_maintenance < timezone.now()
            
            maintenance_data.append({
                'equipment_id': equipment.id,
                'equipment_name': equipment.equipment_name,
                'last_maintenance': equipment.last_maintenance,
                'next_maintenance': next_maintenance,
                'is_overdue': overdue,
                'location': equipment.location
            })
        
        return Response(maintenance_data)

# ===== FINANCIAL MANAGEMENT =====

class ChartOfAccountsViewSet(viewsets.ModelViewSet):
    """Chart of Accounts master data management"""
    serializer_class = ChartOfAccountsSerializer  # Assume exists in serializers.py
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['account_code', 'account_name']
    ordering_fields = ['account_code', 'account_name', 'created_at']
    ordering = ['account_code']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return ChartOfAccounts.objects.filter(tenant=tenant, is_active=True) if tenant else ChartOfAccounts.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)


class GLJournalViewSet(viewsets.ModelViewSet):
    """General Ledger journal entries"""
    serializer_class = GLJournalSerializer
    permission_classes = [IsAuthenticated]
    ordering = ['-posting_date']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return GLJournal.objects.filter(tenant=tenant) if tenant else GLJournal.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()

        # Auto-generate journal number
        last_journal = GLJournal.objects.filter(tenant=tenant).order_by('-id').first()
        journal_number = f"GL-{timezone.now().strftime('%Y%m')}-{(last_journal.id + 1) if last_journal else 1:04d}"

        serializer.save(
            tenant=tenant,
            created_by=self.request.user,
            journal_number=journal_number
        )

    
    @action(detail=True, methods=['post'])
    def post_journal(self, request, pk=None):
        """Post draft journal to GL"""
        journal = self.get_object()
        
        if journal.status != 'draft':
            return Response({'error': 'Only draft journals can be posted'}, status=400)
        
        # Validate debit = credit
        if journal.total_debit != journal.total_credit:
            return Response({'error': 'Debit and credit amounts must be equal'}, status=400)
        
        journal.status = 'posted'
        journal.save()
        
        logger.info(f"Journal {journal.journal_number} posted by {request.user.username}")
        
        return Response({'message': 'Journal posted successfully'})
    
    @action(detail=False, methods=['get'])
    def trial_balance(self, request):
        """Generate trial balance report"""
        tenant = get_current_tenant()
        as_of_date = request.query_params.get('as_of_date', timezone.now().date())
        
        # Get all posted journal lines up to date
        journal_lines = GLJournalLine.objects.filter(
            tenant=tenant,
            journal__status='posted',
            journal__posting_date__lte=as_of_date
        ).select_related('account')
        
        # Aggregate by account
        account_balances = {}
        for line in journal_lines:
            account = line.account
            if account.id not in account_balances:
                account_balances[account.id] = {
                    'account_code': account.account_code,
                    'account_name': account.account_name,
                    'account_type': account.account_type,
                    'debit_total': 0,
                    'credit_total': 0
                }
            
            account_balances[account.id]['debit_total'] += float(line.debit_amount)
            account_balances[account.id]['credit_total'] += float(line.credit_amount)
        
        # Calculate net balances
        for acc_id, balance in account_balances.items():
            balance['net_balance'] = balance['debit_total'] - balance['credit_total']
        
        return Response({
            'as_of_date': as_of_date,
            'account_balances': list(account_balances.values()),
            'total_debits': sum(b['debit_total'] for b in account_balances.values()),
            'total_credits': sum(b['credit_total'] for b in account_balances.values())
        })

# ===== DASHBOARD & REPORTING =====

@api_view(['GET'])
def executive_dashboard(request):
    """Executive summary dashboard"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    today = timezone.now().date()
    
    # Production metrics
    production_summary = {
        'work_orders_active': WorkOrder.objects.filter(
            tenant=tenant, status='in_progress'
        ).count(),
        'daily_production': ProductionEntry.objects.filter(
            tenant=tenant,
            entry_datetime__date=today
        ).aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0,
        'daily_rejections': ProductionEntry.objects.filter(
            tenant=tenant,
            entry_datetime__date=today
        ).aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
    }
    
    # Inventory alerts
    inventory_alerts = []
    low_stock_products = Product.objects.filter(tenant=tenant, is_active=True)
    
    for product in low_stock_products:
        current_stock = StockMovement.objects.filter(
            tenant=tenant, product=product
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        if current_stock <= product.reorder_point:
            inventory_alerts.append({
                'product_sku': product.sku,
                'current_stock': float(current_stock),
                'reorder_point': product.reorder_point,
                'shortage': product.reorder_point - current_stock
            })
    
    # Financial summary (if module enabled)
    financial_summary = {}
    if tenant.modules_enabled.get('finance'):
        recent_journals = GLJournal.objects.filter(
            tenant=tenant,
            status='posted',
            posting_date__gte=today - timedelta(days=30)
        ).count()
        
        financial_summary = {
            'recent_journal_entries': recent_journals,
            'pending_gl_posts': GLJournal.objects.filter(
                tenant=tenant, status='draft'
            ).count()
        }
    
    return Response({
        'production': production_summary,
        'inventory_alerts': inventory_alerts,
        'financial': financial_summary,
        'tenant_info': {
            'company_name': tenant.company_name,
            'active_modules': [k for k, v in tenant.modules_enabled.items() if v]
        }
    })

# ===== EMPLOYEE & COST CENTER MANAGEMENT =====

class EmployeeViewSet(viewsets.ModelViewSet):
    """Employee master with cost center allocation"""
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['employee_code', 'full_name', 'department']
    ordering = ['employee_code']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return Employee.objects.filter(tenant=tenant, is_active=True) if tenant else Employee.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)
    
    # In views.py - Update the productivity_report method in EmployeeViewSet
    @action(detail=False, methods=['get'])
    def productivity_report(self, request):
        """Employee productivity metrics with custom date range"""
        tenant = get_current_tenant()
        
        # Get date range from query parameters (default to all time if not specified)
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date', timezone.now().date())
        
        employees = self.get_queryset()
        productivity_data = []
        
        for employee in employees:
            # Base query for this employee
            production_query = ProductionEntry.objects.filter(
                tenant=tenant,
                operator=employee
            )
            
            # Apply date filters if provided
            if start_date:
                production_query = production_query.filter(entry_datetime__date__gte=start_date)
            if end_date:
                production_query = production_query.filter(entry_datetime__date__lte=end_date)
            
            # Get production metrics
            total_produced = production_query.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
            total_rejected = production_query.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
            total_hours = production_query.count()
            
            productivity_data.append({
                'employee_id': employee.id,
                'employee_code': employee.employee_code,
                'full_name': employee.full_name,
                'department': employee.department,
                'total_produced': total_produced,
                'total_rejected': total_rejected,
                'quality_rate': (total_produced / max(total_produced + total_rejected, 1)) * 100,
                'avg_hourly_output': total_produced / max(total_hours, 1),
                'hours_worked': total_hours
            })
        
        return Response({
            'period': {'start_date': start_date, 'end_date': end_date},
            'employee_productivity': productivity_data
        })
        # In views.py - Add this to EmployeeViewSet
    @action(detail=True, methods=['get'])
    def employee_productivity(self, request, pk=None):
        """Get productivity data for a specific employee with date range filtering"""
        employee = self.get_object()
        tenant = get_current_tenant()
        
        # Get date range from query parameters
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date', timezone.now().date())
        
        # Base query for this employee
        production_query = ProductionEntry.objects.filter(
            tenant=tenant,
            operator=employee
        )
        
        # Apply date filters if provided
        if start_date:
            production_query = production_query.filter(entry_datetime__date__gte=start_date)
        if end_date:
            production_query = production_query.filter(entry_datetime__date__lte=end_date)
        
        # Get detailed production entries
        production_entries = production_query.order_by('-entry_datetime')
        entries_data = ProductionEntrySerializer(production_entries, many=True).data
        
        # Get summary metrics
        total_produced = production_query.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0
        total_rejected = production_query.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0
        total_hours = production_query.count()
        total_downtime = production_query.aggregate(Sum('downtime_minutes'))['downtime_minutes__sum'] or 0
        
        return Response({
            'employee': {
                'id': employee.id,
                'employee_code': employee.employee_code,
                'full_name': employee.full_name,
                'department': employee.department,
                'designation': employee.designation
            },
            'period': {'start_date': start_date, 'end_date': end_date},
            'summary': {
                'total_produced': total_produced,
                'total_rejected': total_rejected,
                'total_hours_worked': total_hours,
                'total_downtime_minutes': total_downtime,
                'quality_rate': (total_produced / max(total_produced + total_rejected, 1)) * 100,
                'avg_hourly_output': total_produced / max(total_hours, 1),
                'avg_downtime_per_shift': total_downtime / max(total_hours, 1)
            },
            'production_entries': entries_data
        })   

class CostCenterViewSet(viewsets.ModelViewSet):
    """Cost center management with budget tracking"""
    serializer_class = CostCenterSerializer
    permission_classes = [IsAuthenticated]
    ordering = ['cost_center_code']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return CostCenter.objects.filter(tenant=tenant, is_active=True) if tenant else CostCenter.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)
    
    @action(detail=True, methods=['get'])
    def cost_analysis(self, request, pk=None):
        """Analyze costs for a cost center"""
        cost_center = self.get_object()
        tenant = get_current_tenant()
        
        # Get GL entries for this cost center
        period_start = request.query_params.get('period_start', (timezone.now() - timedelta(days=30)).date())
        period_end = request.query_params.get('period_end', timezone.now().date())
        
        gl_entries = GLJournalLine.objects.filter(
            tenant=tenant,
            cost_center=cost_center,
            journal__status='posted',
            journal__posting_date__range=[period_start, period_end]
        ).select_related('account', 'journal')
        
        # Aggregate by account type
        cost_breakdown = {}
        total_costs = 0
        
        for entry in gl_entries:
            account_type = entry.account.account_type
            if account_type not in cost_breakdown:
                cost_breakdown[account_type] = 0
            
            # Expenses are debits, revenues are credits
            if account_type in ['expense', 'cogs']:
                amount = float(entry.debit_amount - entry.credit_amount)
                cost_breakdown[account_type] += amount
                total_costs += amount
        
        # Get employee costs for this cost center
        employees_in_cc = Employee.objects.filter(tenant=tenant, cost_center=cost_center)
        labor_costs = 0
        
        for employee in employees_in_cc:
            # This would integrate with payroll module when implemented
            # For now, estimate based on hourly rate and production entries
            recent_entries = ProductionEntry.objects.filter(
                tenant=tenant,
                operator=employee,
                entry_datetime__date__range=[period_start, period_end]
            ).count()
            
            estimated_hours = recent_entries  # 1 entry per hour assumption
            labor_costs += estimated_hours * float(employee.hourly_rate)
        
        return Response({
            'cost_center': {
                'code': cost_center.cost_center_code,
                'name': cost_center.name
            },
            'period': {'start': period_start, 'end': period_end},
            'cost_breakdown': cost_breakdown,
            'estimated_labor_costs': labor_costs,
            'total_costs': total_costs,
            'employee_count': employees_in_cc.count()
        })

# ===== REPORTING & ANALYTICS =====

@api_view(['GET'])
def production_efficiency_report(request):
    """Comprehensive production efficiency analysis"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    date_filter = request.query_params.get('date', timezone.now().date())
    
    # Get all production entries for the date
    entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date=date_filter
    ).select_related('work_order', 'equipment', 'operator')
    
    # Equipment efficiency
    equipment_efficiency = {}
    for entry in entries:
        equip_id = entry.equipment.id
        if equip_id not in equipment_efficiency:
            equipment_efficiency[equip_id] = {
                'equipment_name': entry.equipment.equipment_name,
                'capacity_per_hour': entry.equipment.capacity_per_hour,
                'total_produced': 0,
                'total_rejected': 0,
                'total_hours': 0,
                'downtime_minutes': 0
            }
        
        equipment_efficiency[equip_id]['total_produced'] += entry.quantity_produced
        equipment_efficiency[equip_id]['total_rejected'] += entry.quantity_rejected
        equipment_efficiency[equip_id]['total_hours'] += 1
        equipment_efficiency[equip_id]['downtime_minutes'] += entry.downtime_minutes
    
    # Calculate efficiency percentages
    for equip_data in equipment_efficiency.values():
        if equip_data['capacity_per_hour'] > 0 and equip_data['total_hours'] > 0:
            theoretical_capacity = equip_data['capacity_per_hour'] * equip_data['total_hours']
            equip_data['efficiency_pct'] = (equip_data['total_produced'] / theoretical_capacity) * 100
            equip_data['quality_rate'] = (equip_data['total_produced'] / 
                max(equip_data['total_produced'] + equip_data['total_rejected'], 1)) * 100
            equip_data['availability'] = ((equip_data['total_hours'] * 60 - equip_data['downtime_minutes']) / 
                (equip_data['total_hours'] * 60)) * 100
        else:
            equip_data['efficiency_pct'] = 0
            equip_data['quality_rate'] = 0
            equip_data['availability'] = 0
    
    # Worker efficiency
    worker_efficiency = {}
    for entry in entries:
        worker_id = entry.operator.id
        if worker_id not in worker_efficiency:
            worker_efficiency[worker_id] = {
                'employee_name': entry.operator.full_name,
                'employee_code': entry.operator.employee_code,
                'total_produced': 0,
                'total_rejected': 0,
                'hours_worked': 0
            }
        
        worker_efficiency[worker_id]['total_produced'] += entry.quantity_produced
        worker_efficiency[worker_id]['total_rejected'] += entry.quantity_rejected
        worker_efficiency[worker_id]['hours_worked'] += 1
    
    # Calculate worker efficiency percentages
    for worker_data in worker_efficiency.values():
        total_output = worker_data['total_produced'] + worker_data['total_rejected']
        if total_output > 0:
            worker_data['quality_rate'] = (worker_data['total_produced'] / total_output) * 100
            worker_data['hourly_rate'] = worker_data['total_produced'] / max(worker_data['hours_worked'], 1)
        else:
            worker_data['quality_rate'] = 0
            worker_data['hourly_rate'] = 0
    
    return Response({
        'date': date_filter,
        'equipment_efficiency': list(equipment_efficiency.values()),
        'worker_efficiency': list(worker_efficiency.values()),
        'summary': {
            'total_production': sum(e['total_produced'] for e in equipment_efficiency.values()),
            'total_rejections': sum(e['total_rejected'] for e in equipment_efficiency.values()),
            'avg_equipment_efficiency': sum(e['efficiency_pct'] for e in equipment_efficiency.values()) / max(len(equipment_efficiency), 1),
            'active_equipment': len(equipment_efficiency),
            'active_workers': len(worker_efficiency)
        }
    })

@api_view(['POST'])
def quick_stock_adjustment(request):
    """Quick stock adjustment for cycle counting"""
    tenant = get_current_tenant()
    data = request.data
    
    required_fields = ['product_id', 'warehouse_id', 'actual_quantity', 'reason']
    if not all(field in data for field in required_fields):
        return Response({'error': 'Missing required fields'}, status=400)
    
    try:
        with transaction.atomic():
            product = get_object_or_404(Product, id=data['product_id'], tenant=tenant)
            warehouse = get_object_or_404(Warehouse, id=data['warehouse_id'], tenant=tenant)
            
            # Calculate current system stock
            system_stock = StockMovement.objects.filter(
                tenant=tenant,
                product=product,
                warehouse=warehouse
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            actual_quantity = Decimal(str(data['actual_quantity']))
            adjustment_qty = actual_quantity - system_stock
            
            if adjustment_qty != 0:
                # Create adjustment movement
                movement_number = generate_movement_number(tenant, 'ADJ')
                
                StockMovement.objects.create(
                    tenant=tenant,
                    movement_number=movement_number,
                    movement_type='adjustment',
                    product=product,
                    warehouse=warehouse,
                    quantity=adjustment_qty,
                    unit_cost=product.standard_cost,
                    reference_doc=f"Cycle Count - {data['reason']}",
                    movement_date=timezone.now(),
                    created_by=request.user
                )
                
                # Clear cache
                cache.delete(f"stock_{tenant.id}_{product.id}")
                
                logger.info(f"Stock adjustment: {product.sku} adjusted by {adjustment_qty}")
            
            return Response({
                'message': 'Stock adjustment completed',
                'system_stock': float(system_stock),
                'actual_stock': float(actual_quantity),
                'adjustment_quantity': float(adjustment_qty),
                'movement_number': movement_number if adjustment_qty != 0 else None
            })
            
    except Exception as e:
        logger.error(f"Stock adjustment failed: {str(e)}")
        return Response({'error': 'Adjustment failed'}, status=500)

# ===== DATA IMPORT/EXPORT =====

@api_view(['POST'])
def import_csv_data(request):
    """Generic CSV import with field mapping"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    data_type = request.data.get('data_type')  # 'products', 'employees', 'production_entries'
    csv_file = request.FILES.get('csv_file')
    field_mapping = request.data.get('field_mapping', {})
    
    if not csv_file or not data_type:
        return Response({'error': 'CSV file and data_type required'}, status=400)
    
    try:
        import csv
        import io
        
        # Parse CSV
        csv_data = csv_file.read().decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(csv_data))
        
        created_count = 0
        errors = []
        
        with transaction.atomic():
            for row_num, row in enumerate(csv_reader, 1):
                try:
                    # Map fields based on provided mapping
                    mapped_data = {}
                    for csv_field, model_field in field_mapping.items():
                        if csv_field in row:
                            mapped_data[model_field] = row[csv_field]
                    
                    # Add tenant context
                    mapped_data['tenant'] = tenant.id
                    
                    # Create object based on data_type
                    if data_type == 'products':
                        serializer = ProductSerializer(data=mapped_data)
                    elif data_type == 'employees':
                        serializer = EmployeeSerializer(data=mapped_data)
                    else:
                        errors.append(f"Row {row_num}: Unsupported data type")
                        continue
                    
                    if serializer.is_valid():
                        serializer.save(created_by=request.user)
                        created_count += 1
                    else:
                        errors.append(f"Row {row_num}: {serializer.errors}")
                        
                except Exception as e:
                    errors.append(f"Row {row_num}: {str(e)}")
        
        return Response({
            'message': f'Import completed: {created_count} records created',
            'created_count': created_count,
            'errors': errors[:10]  # Limit error list
        })
        
    except Exception as e:
        logger.error(f"CSV import failed: {str(e)}")
        return Response({'error': 'Import failed'}, status=500)

@api_view(['GET'])
def export_data(request):
    """Export data in various formats"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    export_type = request.query_params.get('type')  # 'stock_report', 'production_summary'
    format_type = request.query_params.get('format', 'json')  # 'json', 'csv'
    
    if export_type == 'stock_report':
        # Generate stock report
        stock_data = []
        products = Product.objects.filter(tenant=tenant, is_active=True)
        
        for product in products:
            current_stock = StockMovement.objects.filter(
                tenant=tenant, product=product
            ).aggregate(Sum('quantity'))['quantity__sum'] or 0
            
            stock_data.append({
                'sku': product.sku,
                'product_name': product.product_name,
                'current_stock': float(current_stock),
                'reorder_point': product.reorder_point,
                'standard_cost': float(product.standard_cost),
                'stock_value': float(current_stock * product.standard_cost)
            })
        
        if format_type == 'csv':
            # Return CSV format (simplified - in production you'd use proper CSV response)
            return Response({
                'export_type': 'stock_report',
                'format': 'csv',
                'data': stock_data,
                'filename': f'stock_report_{timezone.now().strftime("%Y%m%d")}.csv'
            })
        
        return Response({
            'export_type': 'stock_report',
            'generated_at': timezone.now(),
            'total_products': len(stock_data),
            'total_stock_value': sum(item['stock_value'] for item in stock_data),
            'data': stock_data
        })
    
    elif export_type == 'production_summary':
        # Generate production summary
        date_filter = request.query_params.get('date', timezone.now().date())
        
        production_entries = ProductionEntry.objects.filter(
            tenant=tenant,
            entry_datetime__date=date_filter
        ).select_related('work_order', 'equipment', 'operator')
        
        summary_data = []
        for entry in production_entries:
            summary_data.append({
                'work_order': entry.work_order.wo_number,
                'product_sku': entry.work_order.product.sku,
                'equipment': entry.equipment.equipment_name,
                'operator': entry.operator.full_name,
                'shift': entry.shift,
                'quantity_produced': entry.quantity_produced,
                'quantity_rejected': entry.quantity_rejected,
                'downtime_minutes': entry.downtime_minutes,
                'entry_time': entry.entry_datetime.strftime('%H:%M')
            })
        
        return Response({
            'export_type': 'production_summary',
            'date': date_filter,
            'total_entries': len(summary_data),
            'data': summary_data
        })
    
    return Response({'error': 'Invalid export type'}, status=400)

# ===== BUSINESS INTELLIGENCE VIEWS =====

@api_view(['GET'])
def kpi_dashboard(request):
    """Key Performance Indicators dashboard"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Date range for analysis
    end_date = timezone.now().date()
    start_date = end_date - timedelta(days=30)
    
    # Production KPIs
    production_entries = ProductionEntry.objects.filter(
        tenant=tenant,
        entry_datetime__date__range=[start_date, end_date]
    )
    
    production_kpis = {
        'total_production': production_entries.aggregate(Sum('quantity_produced'))['quantity_produced__sum'] or 0,
        'total_rejections': production_entries.aggregate(Sum('quantity_rejected'))['quantity_rejected__sum'] or 0,
        'total_downtime': production_entries.aggregate(Sum('downtime_minutes'))['downtime_minutes__sum'] or 0,
        'avg_quality_rate': 0,
        'avg_oee': 0
    }
    
    # Calculate quality rate
    total_good = production_kpis['total_production']
    total_bad = production_kpis['total_rejections']
    if total_good + total_bad > 0:
        production_kpis['avg_quality_rate'] = (total_good / (total_good + total_bad)) * 100
    
    # Inventory KPIs
    total_stock_value = 0
    products_below_reorder = 0
    
    for product in Product.objects.filter(tenant=tenant, is_active=True):
        current_stock = StockMovement.objects.filter(
            tenant=tenant, product=product
        ).aggregate(Sum('quantity'))['quantity__sum'] or 0
        
        total_stock_value += current_stock * product.standard_cost
        
        if current_stock <= product.reorder_point:
            products_below_reorder += 1
    
    inventory_kpis = {
        'total_stock_value': float(total_stock_value),
        'products_below_reorder': products_below_reorder,
        'total_products': Product.objects.filter(tenant=tenant, is_active=True).count()
    }
    
    # Work Order KPIs
    work_orders = WorkOrder.objects.filter(tenant=tenant, is_active=True)
    wo_kpis = {
        'total_work_orders': work_orders.count(),
        'completed_orders': work_orders.filter(status='completed').count(),
        'overdue_orders': work_orders.filter(
            due_date__lt=end_date,
            status__in=['planned', 'in_progress']
        ).count(),
        'in_progress_orders': work_orders.filter(status='in_progress').count()
    }
    
    return Response({
        'period': {'start_date': start_date, 'end_date': end_date},
        'production_kpis': production_kpis,
        'inventory_kpis': inventory_kpis,
        'work_order_kpis': wo_kpis,
        'summary_score': calculate_overall_performance_score(production_kpis, inventory_kpis, wo_kpis)
    })

def calculate_overall_performance_score(production_kpis, inventory_kpis, wo_kpis):
    """Calculate overall performance score (0-100)"""
    scores = []
    
    # Production score (quality rate)
    if production_kpis['avg_quality_rate'] > 0:
        scores.append(min(100, production_kpis['avg_quality_rate']))
    
    # Work order completion score
    if wo_kpis['total_work_orders'] > 0:
        completion_rate = (wo_kpis['completed_orders'] / wo_kpis['total_work_orders']) * 100
        scores.append(completion_rate)
    
    # Inventory management score (inverse of stockout rate)
    if inventory_kpis['total_products'] > 0:
        stockout_rate = (inventory_kpis['products_below_reorder'] / inventory_kpis['total_products']) * 100
        inventory_score = max(0, 100 - stockout_rate * 2)  # Penalize stockouts
        scores.append(inventory_score)
    
    return sum(scores) / max(len(scores), 1) if scores else 0

# ===== UTILITY ENDPOINTS =====

@api_view(['GET'])
def system_health(request):
    """System health check for monitoring"""
    tenant = get_current_tenant()
    
    health_data = {
        'status': 'healthy',
        'timestamp': timezone.now(),
        'tenant_active': tenant is not None,
        'database_connected': True,  # Will be False if DB query fails
        'cache_working': False
    }
    
    # Test cache
    try:
        cache.set('health_check', 'ok', timeout=60)
        health_data['cache_working'] = cache.get('health_check') == 'ok'
    except:
        pass
    
    # Test database with simple query
    try:
        Tenant.objects.count()
    except:
        health_data['database_connected'] = False
        health_data['status'] = 'unhealthy'
    
    return Response(health_data)

@api_view(['GET'])
def audit_trail(request):
    """Recent system activities for audit"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Get recent activities across modules
    recent_activities = []
    
    # Recent work orders
    recent_wos = WorkOrder.objects.filter(
        tenant=tenant,
        created_at__gte=timezone.now() - timedelta(days=7)
    ).order_by('-created_at')[:5]
    
    for wo in recent_wos:
        recent_activities.append({
            'timestamp': wo.created_at,
            'activity_type': 'work_order_created',
            'description': f"Work Order {wo.wo_number} created for {wo.product.sku}",
            'user': wo.created_by.username if wo.created_by else 'System'
        })
    
    # Recent stock movements
    recent_movements = StockMovement.objects.filter(
        tenant=tenant,
        created_at__gte=timezone.now() - timedelta(days=7)
    ).order_by('-created_at')[:5]
    
    for movement in recent_movements:
        recent_activities.append({
            'timestamp': movement.created_at,
            'activity_type': 'stock_movement',
            'description': f"{movement.movement_type.title()}: {movement.product.sku} ({movement.quantity})",
            'user': movement.created_by.username if movement.created_by else 'System'
        })
    
    # Sort by timestamp
    recent_activities.sort(key=lambda x: x['timestamp'], reverse=True)
    
    return Response({
        'activities': recent_activities[:10],
        'total_activities': len(recent_activities)
    })

class WarehouseViewSet(viewsets.ModelViewSet):
    """Warehouse master data management"""
    serializer_class = WarehouseSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['warehouse_code', 'warehouse_name', 'location']
    ordering_fields = ['warehouse_code', 'warehouse_name', 'created_at']
    ordering = ['warehouse_code']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return Warehouse.objects.filter(tenant=tenant, is_active=True) if tenant else Warehouse.objects.none()
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)

# core/views.py - Add at the bottom
from django.shortcuts import render

def ai_test_view(request):
    """Render the AI test template"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    return render(request, 'ai_test.html', {'csrf_token': request.META.get('CSRF_COOKIE', '')})

    # Placeholder for LLM integration (as discussed - for enhanced reasoning)


class AIQueryView(APIView):
    """Simple AI Query API endpoint"""
    permission_classes = [permissions.IsAuthenticated]
    
    def post(self, request, *args, **kwargs):
        """Process AI query"""
        from .middleware import get_current_tenant
        
        tenant = get_current_tenant()
        if not tenant:
            return Response({
                'error': 'No tenant context',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        query = request.data.get('query', '').strip()
        if not query:
            return Response({
                'error': 'Query is required',
                'success': False
            }, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # Process query using AI engine
            ai_engine = ERPAIEngine(tenant, request.user)
            result = ai_engine.process_query(query)
            
            # Log query for analytics
            try:
                AIQueryLog.objects.create(
                    tenant=tenant,
                    user_query=query,
                    was_successful=result.get('success', False),
                    result_rows=self._count_rows(result.get('data', {})),
                    created_by=request.user
                )
            except Exception as log_error:
                logger.warning(f"Failed to log query: {log_error}")
            
            return Response(result, status=status.HTTP_200_OK)
            
        except Exception as e:
            logger.error(f"AI query processing error: {e}", exc_info=True)
            
            # Log failed query
            try:
                AIQueryLog.objects.create(
                    tenant=tenant,
                    user_query=query,
                    was_successful=False,
                    error_message=str(e)[:500],
                    created_by=request.user
                )
            except:
                pass
            
            return Response({
                'success': False,
                'error': 'Query processing failed',
                'response': 'I encountered an issue processing your request. Please try again or rephrase your question.'
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def get(self, request, *args, **kwargs):
        """Get AI capabilities and examples"""
        from .middleware import get_current_tenant
        
        tenant = get_current_tenant()
        if not tenant:
            return Response({'error': 'No tenant context'}, status=400)
        
        return Response({
            'message': 'ERP AI Assistant Ready',
            'capabilities': [
                'Product and inventory management queries',
                'Production analysis and insights',
                'Equipment performance monitoring', 
                'Work order tracking and analysis',
                'Employee productivity insights',
                'Quality and defect analysis',
                'Business intelligence and reporting'
            ],
            'example_queries': [
                'Show me all products that need reordering',
                'List employees in production department',
                'Which equipment needs maintenance?',
                'Show overdue work orders',
                'Why was production low last month?',
                'Analyze quality issues this week',
                'What equipment has the most downtime?',
                'Which products have high rejection rates?'
            ],
            'company': tenant.company_name
        })
    
    def _count_rows(self, data: Dict[str, Any]) -> int:
        """Count rows in data for logging"""
        total_rows = 0
        
        if isinstance(data, list):
            return len(data)
        
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list):
                    total_rows += len(value)
                elif isinstance(value, dict):
                    if 'summary' in value:
                        total_rows += 1
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, list):
                            total_rows += len(sub_value)
        
        return total_rows
        
class PurchaseOrderViewSet(viewsets.ModelViewSet):
    """Purchase Order management - optional for supplier orders with simple amount"""
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['po_number', 'supplier__display_name']
    ordering_fields = ['po_number', 'order_date', 'status']
    ordering = ['-order_date']
    
    # Add this line to fix the basename error
    queryset = PurchaseOrder.objects.all()  # Will be filtered by tenant in get_queryset
    
    def get_queryset(self):
        tenant = get_current_tenant()
        return PurchaseOrder.objects.filter(tenant=tenant, is_active=True) if tenant else PurchaseOrder.objects.none()
    
    def perform_create(self, serializer):
        serializer.save()
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context
    
    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Mark PO as sent to supplier"""
        po = self.get_object()
        if po.status != 'draft':
            return Response({'error': 'Only draft POs can be sent'}, status=400)
        
        po.status = 'sent'
        po.save()
        
        return Response({'message': 'PO marked as sent', 'status': po.status})
    
    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Mark PO as received and create stock receipts"""
        po = self.get_object()
        if po.status != 'sent':
            return Response({'error': 'Only sent POs can be received'}, status=400)
        
        warehouse = Warehouse.objects.filter(tenant=po.tenant).first()
        if not warehouse:
            return Response({'error': 'No warehouse found for receipts'}, status=400)
        
        with transaction.atomic():
            po.status = 'received'
            po.save()
            
            for line in po.lines.all():
                StockMovement.objects.create(
                    tenant=po.tenant,
                    movement_number=generate_movement_number(po.tenant, 'PO-RECV'),
                    movement_type='receipt',
                    product=line.product,
                    warehouse=warehouse,
                    quantity=line.quantity,
                    unit_cost=line.unit_price,
                    reference_doc=po.po_number,
                    movement_date=timezone.now(),
                    created_by=request.user
                )
            
            if po.tenant.modules_enabled.get('finance'):
                create_automated_gl_entry(
                    po.tenant,
                    'purchase_receipt',
                    {'purchase_order_id': po.id},
                    user=request.user
                )
        
        for line in po.lines.all():
            cache.delete(f"stock_{po.tenant.id}_{line.product.id}")
        
        return Response({'message': 'PO received and stock updated', 'status': po.status})
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel PO if not received"""
        po = self.get_object()
        if po.status in ['received', 'cancelled']:
            return Response({'error': 'Cannot cancel received or already cancelled PO'}, status=400)
        
        po.status = 'cancelled'
        po.save()
        
        return Response({'message': 'PO cancelled', 'status': po.status})

    @action(detail=True, methods=['post'], parser_classes=[parsers.MultiPartParser])
    def upload_document(self, request, pk=None):
        """Upload PO document"""
        po = self.get_object()
        
        if 'document' not in request.FILES:
            return Response({'error': 'No document file provided'}, status=400)
        
        if po.po_document:
            po.po_document.delete(save=False)
        
        po.po_document = request.FILES['document']
        po.save()
        
        return Response({
            'message': 'Document uploaded successfully',
            'document_url': po.po_document.url if po.po_document else None
        })    

    @action(detail=True, methods=['get'])
    def download_pdf(self, request, pk=None):
        """Generate and download PO as PDF with enhanced UI"""
        po = self.get_object()
        tenant = po.tenant
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            topMargin=0.5*inch,
            bottomMargin=0.5*inch,
            leftMargin=0.5*inch,
            rightMargin=0.5*inch
        )
        elements = []
        styles = getSampleStyleSheet()
        
        # Custom styles with unique names to avoid conflicts
        if 'CustomCompanyHeader' not in styles:
            styles.add(ParagraphStyle(
                name='CustomCompanyHeader',
                fontName='Helvetica-Bold',
                fontSize=16,
                textColor=colors.HexColor('#9333EA'),  # Purple from StockReports.jsx
                spaceAfter=6
            ))
        if 'CustomSubHeader' not in styles:
            styles.add(ParagraphStyle(
                name='CustomSubHeader',
                fontName='Helvetica',
                fontSize=10,
                textColor=colors.HexColor('#6B7280'),  # Gray-400
                spaceAfter=4
            ))
        if 'CustomBodyText' not in styles:
            styles.add(ParagraphStyle(
                name='CustomBodyText',
                fontName='Helvetica',
                fontSize=10,
                textColor=colors.black,
                spaceAfter=4
            ))
        if 'CustomSectionTitle' not in styles:
            styles.add(ParagraphStyle(
                name='CustomSectionTitle',
                fontName='Helvetica-Bold',
                fontSize=12,
                textColor=colors.HexColor('#2563EB'),  # Blue-600
                spaceBefore=12,
                spaceAfter=6
            ))

        # Header
        elements.append(Paragraph(tenant.company_name, styles['CustomCompanyHeader']))
        elements.append(Paragraph(tenant.company_address or "Address not specified", styles['CustomSubHeader']))
        elements.append(Paragraph(f"GSTIN: {tenant.gstin or 'N/A'}", styles['CustomSubHeader']))
        elements.append(Spacer(1, 0.25*inch))
        
        # PO Title and Details
        elements.append(Paragraph("Purchase Order", styles['CustomSectionTitle']))
        elements.append(Paragraph(f"PO Number: {po.po_number}", styles['CustomBodyText']))
        elements.append(Paragraph(f"Date: {po.order_date}", styles['CustomBodyText']))
        elements.append(Paragraph(f"Expected Delivery: {po.expected_delivery or 'N/A'}", styles['CustomBodyText']))
        elements.append(Spacer(1, 0.25*inch))
        
        # Supplier Details
        elements.append(Paragraph("Supplier Details", styles['CustomSectionTitle']))
        elements.append(Paragraph(po.supplier.display_name, styles['CustomBodyText']))
        elements.append(Paragraph(f"GSTIN: {po.supplier.gstin or 'N/A'}", styles['CustomBodyText']))

        # Handle contact_details safely
        address = "N/A"
        try:
            if isinstance(po.supplier.contact_details, dict):
                address = po.supplier.contact_details.get('address', 'N/A')
            elif isinstance(po.supplier.contact_details, str):
                try:
                    contact_details = json.loads(po.supplier.contact_details)
                    address = contact_details.get('address', 'N/A')
                except json.JSONDecodeError:
                    address = po.supplier.contact_details
            logger.info(f"contact_details: {po.supplier.contact_details}, type: {type(po.supplier.contact_details)}, parsed address: {address}")
        except Exception as e:
            logger.error(f"Error processing contact_details for supplier {po.supplier.id}: {str(e)}")
        
        elements.append(Paragraph(f"Address: {address}", styles['CustomBodyText']))
        elements.append(Spacer(1, 0.25*inch))
        
        # Items Table
        data = [['Line', 'Product', 'Quantity', 'Unit Price', 'Subtotal']]
        for line in po.lines.all():
            data.append([
                str(line.line_number),
                line.product.product_name,
                f"{line.quantity} {line.product.uom}",
                f"{line.unit_price:.2f}",
                f"{line.subtotal:.2f}"
            ])
        
        table = Table(data, colWidths=[0.5*inch, 2.5*inch, 1*inch, 1*inch, 1*inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#9333EA')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F3F4F6')),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D1D5DB')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#6B7280')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#F3F4F6'), colors.HexColor('#E5E7EB')]),
        ]))
        elements.append(table)
        
        # Total Amount
        elements.append(Spacer(1, 0.25*inch))
        elements.append(Paragraph(f"Total Amount: {po.amount:.2f}", styles['CustomSectionTitle']))
        
        # Terms & Conditions
        if po.terms_conditions:
            elements.append(Paragraph("Terms & Conditions", styles['CustomSectionTitle']))
            elements.append(Paragraph(po.terms_conditions, styles['CustomBodyText']))
        
        # Footer
        def add_footer(canvas, doc):
            canvas.saveState()
            canvas.setFont('Helvetica', 8)
            canvas.setFillColor(colors.HexColor('#6B7280'))
            canvas.drawString(0.5*inch, 0.3*inch, f"{tenant.company_name} | Page {doc.page}")
            canvas.restoreState()
        
        doc.build(elements, onFirstPage=add_footer, onLaterPages=add_footer)
        buffer.seek(0)
        
        response = HttpResponse(buffer, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="PO_{po.po_number}.pdf"'
        return response

class EmployeeDocumentViewSet(viewsets.ModelViewSet):
    serializer_class = EmployeeDocumentSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]
    
    def get_queryset(self):
        tenant = get_current_tenant()
        queryset = EmployeeDocument.objects.filter(tenant=tenant) if tenant else EmployeeDocument.objects.none()
        
        employee_id = self.request.query_params.get('employee_id')
        if employee_id:
            queryset = queryset.filter(employee_id=employee_id)
        
        return queryset.order_by('-created_at')
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        serializer.save(tenant=tenant, created_by=self.request.user)


class CustomerInvoiceViewSet(viewsets.ModelViewSet):
    """
    Single endpoint for create/read/update/delete invoices,
    supports multipart uploads (invoice_document) on create & update.
    """
    serializer_class = CustomerInvoiceSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]

    def get_queryset(self):
        tenant = get_current_tenant()
        return CustomerInvoice.objects.filter(tenant=tenant, is_active=True) if tenant else CustomerInvoice.objects.none()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        # ensure request is present for serializer to pick up 'amount' and created_by
        ctx['request'] = self.request
        return ctx

    def perform_create(self, serializer):
        tenant = get_current_tenant()
        # serializer.create will also ensure tenant + created_by, but save here as safety
        serializer.save(tenant=tenant, created_by=self.request.user)

    def perform_update(self, serializer):
        # serializer.update handles file replacement cleanup
        serializer.save()

    @action(detail=True, methods=['post'], parser_classes=[parsers.MultiPartParser], url_path='upload-document')
    def upload_document(self, request, pk=None):
        """
        Convenience endpoint: upload/replace the invoice_document only.
        Clients can also PATCH the invoice with 'invoice_document' via multipart form.
        """
        invoice = self.get_object()
        if 'invoice_document' not in request.FILES:
            return Response({'error': 'No document provided'}, status=status.HTTP_400_BAD_REQUEST)

        document = request.FILES['invoice_document']
        if not document.name.lower().endswith('.pdf'):
            return Response({'error': 'Only PDF files are allowed'}, status=status.HTTP_400_BAD_REQUEST)

        # delete existing file if present
        if invoice.invoice_document:
            try:
                invoice.invoice_document.delete(save=False)
            except Exception:
                logger.exception("Failed to delete previous invoice_document")

        invoice.invoice_document = document
        invoice.save()
        return Response({'message': 'Invoice document uploaded successfully', 'document_url': invoice.invoice_document.url})
class CustomerPurchaseOrderViewSet(viewsets.ModelViewSet):
    """Customer Purchase Order management - POs received from customers"""
    serializer_class = CustomerPurchaseOrderSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['po_number', 'customer__display_name', 'customer__party_code']
    ordering_fields = ['po_date', 'status', 'created_at']
    ordering = ['-po_date']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        
        if not tenant:
            return CustomerPurchaseOrder.objects.none()
        
        # Only return active records
        queryset = CustomerPurchaseOrder.objects.filter(tenant=tenant, is_active=True)
        
        # Filter by status
        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        
        # Filter by customer
        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        if start_date:
            queryset = queryset.filter(po_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(po_date__lte=end_date)
        
        return queryset.select_related('customer')
    
    def perform_create(self, serializer):
        """Ensure proper creation with tenant and user context"""
        tenant = get_current_tenant()
        if not tenant:
            raise serializers.ValidationError("No tenant context")
        
        # Let the serializer handle the creation with proper context
        serializer.save()
    
    @action(detail=True, methods=['post'], parser_classes=[parsers.MultiPartParser])
    def upload_document(self, request, pk=None):
        """Upload customer PO document"""
        customer_po = self.get_object()
        
        if 'document' not in request.FILES:
            return Response({'error': 'No document file provided'}, status=400)
        
        document = request.FILES['document']
        
        # Validate file type
        allowed_extensions = ['.pdf', '.docx', '.xlsx', '.doc', '.xls', '.jpg', '.jpeg', '.png']
        file_extension = os.path.splitext(document.name)[1].lower()
        if file_extension not in allowed_extensions:
            return Response({
                'error': f'Invalid file type. Allowed types: {", ".join(allowed_extensions)}'
            }, status=400)
        
        # Delete old document if exists
        if customer_po.po_document:
            try:
                customer_po.po_document.delete(save=False)
            except Exception as e:
                logger.warning(f"Failed to delete old document: {e}")
        
        customer_po.po_document = document
        customer_po.save()
        
        return Response({
            'message': 'Customer PO document uploaded successfully',
            'document_url': customer_po.po_document.url if customer_po.po_document else None
        })
    
    @action(detail=True, methods=['delete'])
    def delete_document(self, request, pk=None):
        """Delete customer PO document"""
        customer_po = self.get_object()
        
        if not customer_po.po_document:
            return Response({'error': 'No document to delete'}, status=400)
        
        try:
            customer_po.po_document.delete(save=False)
            customer_po.po_document = None
            customer_po.save()
            return Response({'message': 'Document deleted successfully'})
        except Exception as e:
            logger.error(f"Failed to delete document: {e}")
            return Response({'error': 'Failed to delete document'}, status=500)
    
    @action(detail=True, methods=['post'])
    def acknowledge(self, request, pk=None):
        """Acknowledge receipt of customer PO"""
        customer_po = self.get_object()
        
        if customer_po.status != 'received':
            return Response({'error': 'Only received POs can be acknowledged'}, status=400)
        
        customer_po.status = 'acknowledged'
        customer_po.save()
        
        logger.info(f"Customer PO {customer_po.po_number} acknowledged by {request.user.username}")
        
        return Response({'message': 'Customer PO acknowledged', 'status': customer_po.status})
    
    @action(detail=True, methods=['post'])
    def start_processing(self, request, pk=None):
        """Mark customer PO as in progress"""
        customer_po = self.get_object()
        
        if customer_po.status not in ['received', 'acknowledged']:
            return Response({'error': 'Invalid status transition'}, status=400)
        
        customer_po.status = 'in_progress'
        customer_po.save()
        
        return Response({'message': 'Customer PO processing started', 'status': customer_po.status})
    
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        """Mark customer PO as completed"""
        customer_po = self.get_object()
        
        if customer_po.status != 'in_progress':
            return Response({'error': 'Only in-progress POs can be completed'}, status=400)
        
        customer_po.status = 'completed'
        customer_po.save()
        
        return Response({'message': 'Customer PO completed', 'status': customer_po.status})
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel customer PO"""
        customer_po = self.get_object()
        
        if customer_po.status in ['completed', 'cancelled']:
            return Response({'error': 'Cannot cancel completed or already cancelled PO'}, status=400)
        
        customer_po.status = 'cancelled'
        customer_po.save()
        
        return Response({'message': 'Customer PO cancelled', 'status': customer_po.status})
    
    @action(detail=False, methods=['get'])
    def status_summary(self, request):
        """Get count of POs by status"""
        tenant = get_current_tenant()
        if not tenant:
            return Response({'error': 'No tenant context'}, status=400)
        
        status_counts = CustomerPurchaseOrder.objects.filter(
            tenant=tenant, 
            is_active=True
        ).values('status').annotate(count=Count('id'))
        
        summary = {item['status']: item['count'] for item in status_counts}
        
        # Include all possible statuses with 0 count if not present
        all_statuses = ['received', 'acknowledged', 'in_progress', 'completed', 'cancelled']
        for status in all_statuses:
            if status not in summary:
                summary[status] = 0
        
        return Response(summary)

class PaymentAdviceViewSet(viewsets.ModelViewSet):
    """Payment Advice management - track customer payments"""
    serializer_class = PaymentAdviceSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['advice_number', 'customer__display_name']
    ordering_fields = ['advice_date', 'total_payment_amount']
    ordering = ['-advice_date']
    
    def get_queryset(self):
        tenant = get_current_tenant()
        queryset = PaymentAdvice.objects.filter(tenant=tenant, is_active=True) if tenant else PaymentAdvice.objects.none()
        
        # Filter by customer
        customer_id = self.request.query_params.get('customer_id')
        if customer_id:
            queryset = queryset.filter(customer_id=customer_id)
        
        # Filter by date range
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        
        if start_date:
            queryset = queryset.filter(advice_date__gte=start_date)
        if end_date:
            queryset = queryset.filter(advice_date__lte=end_date)
        
        return queryset.select_related('customer').prefetch_related('mentioned_invoices')
    
    def perform_create(self, serializer):
        tenant = get_current_tenant()
        
        # Auto-generate advice number if not provided
        if not serializer.validated_data.get('advice_number'):
            last_advice = PaymentAdvice.objects.filter(tenant=tenant).order_by('-id').first()
            advice_number = f"PA-{timezone.now().strftime('%Y%m')}-{(last_advice.id + 1) if last_advice else 1:04d}"
            serializer.validated_data['advice_number'] = advice_number
        
        serializer.save(tenant=tenant, created_by=self.request.user)
    
    @action(detail=True, methods=['post'], parser_classes=[parsers.MultiPartParser])
    def upload_document(self, request, pk=None):
        """Upload payment advice document"""
        payment_advice = self.get_object()
        
        if 'document' not in request.FILES:
            return Response({'error': 'No document file provided'}, status=400)
        
        # Delete old document if exists
        if payment_advice.advice_document:
            payment_advice.advice_document.delete(save=False)
        
        payment_advice.advice_document = request.FILES['document']
        payment_advice.save()
        
        return Response({
            'message': 'Payment advice document uploaded successfully',
            'document_url': payment_advice.advice_document.url if payment_advice.advice_document else None
        })
    
    @action(detail=True, methods=['delete'])
    def delete_document(self, request, pk=None):
        """Delete payment advice document"""
        payment_advice = self.get_object()
        
        if payment_advice.advice_document:
            payment_advice.advice_document.delete(save=True)
            return Response({'message': 'Document deleted successfully'})
        
        return Response({'error': 'No document to delete'}, status=400)
    
    @action(detail=True, methods=['post'])
    def link_invoices(self, request, pk=None):
        """Link invoices mentioned in payment advice"""
        payment_advice = self.get_object()
        invoice_links = request.data.get('invoices', [])  # [{'invoice_id': 1, 'amount': 1000}, ...]
        
        if not invoice_links:
            return Response({'error': 'No invoices provided'}, status=400)
        
        try:
            with transaction.atomic():
                # Clear existing links
                PaymentAdviceInvoice.objects.filter(payment_advice=payment_advice).delete()
                
                # Create new links
                for link in invoice_links:
                    invoice = get_object_or_404(
                        CustomerInvoice, 
                        id=link['invoice_id'], 
                        tenant=payment_advice.tenant
                    )
                    
                    PaymentAdviceInvoice.objects.create(
                        tenant=payment_advice.tenant,
                        payment_advice=payment_advice,
                        invoice=invoice,
                        amount_mentioned=Decimal(str(link['amount'])),
                        created_by=request.user
                    )
                
                return Response({
                    'message': f'{len(invoice_links)} invoices linked successfully',
                    'linked_count': len(invoice_links)
                })
                
        except Exception as e:
            logger.error(f"Invoice linking failed: {str(e)}")
            return Response({'error': 'Failed to link invoices'}, status=500)
    
    @action(detail=True, methods=['get'])
    def invoice_allocation(self, request, pk=None):
        """Get payment allocation details"""
        payment_advice = self.get_object()
        
        allocations = PaymentAdviceInvoice.objects.filter(
            payment_advice=payment_advice
        ).select_related('invoice')
        
        allocation_data = []
        for alloc in allocations:
            allocation_data.append({
                'invoice_number': alloc.invoice.invoice_number,
                'invoice_date': alloc.invoice.invoice_date,
                'invoice_amount': float(alloc.invoice.invoice_amount),
                'amount_mentioned': float(alloc.amount_mentioned),
                'invoice_status': alloc.invoice.status
            })
        
        return Response({
            'payment_advice_number': payment_advice.advice_number,
            'total_payment': float(payment_advice.total_payment_amount),
            'total_allocated': sum(float(a.amount_mentioned) for a in allocations),
            'allocations': allocation_data
        })
# Add these fixed views to your views.py

from datetime import datetime, timedelta
from decimal import Decimal
import json

class PaymentAdviceReconcileView(APIView):
    """Reconcile payment advice with customer invoices - Manual Only"""
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]
    
    def post(self, request, *args, **kwargs):
        """Process payment advice and reconcile with invoices - Manual Only"""
        tenant = get_current_tenant()
        if not tenant:
            return Response({'error': 'No tenant context'}, status=400)
        
        customer_id = request.data.get('customer_id')
        
        if not customer_id:
            return Response({'error': 'Customer ID is required'}, status=400)
        
        try:
            customer = Party.objects.get(id=customer_id, tenant=tenant, party_type='customer')
        except Party.DoesNotExist:
            return Response({'error': 'Customer not found'}, status=404)
        
        reconciliation_service = ReconciliationService(tenant)
        return self._process_manual_reconciliation(request, customer, reconciliation_service)
    
    def _process_manual_reconciliation(self, request, customer, reconciliation_service):
        """Process manual reconciliation"""
        manual_invoice_numbers = request.data.get('invoice_numbers', [])
        
        if not manual_invoice_numbers:
            return Response({'error': 'No invoice numbers provided'}, status=400)
        
        try:
            reconciliation_result = reconciliation_service.reconcile_manual_data(
                customer=customer,
                manual_invoice_numbers=manual_invoice_numbers
            )
            
            return Response({
                'success': True,
                'reconciliation': reconciliation_result,
                'message': 'Manual reconciliation completed successfully'
            })
            
        except Exception as e:
            logger.error(f"Manual reconciliation failed: {e}", exc_info=True)
            return Response({
                'success': False,
                'error': f'Reconciliation failed: {str(e)}'
            }, status=500)

class CustomerInvoiceManualCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request, *args, **kwargs):
        tenant = get_current_tenant()
        if not tenant:
            return Response({'error': 'No tenant context'}, status=status.HTTP_400_BAD_REQUEST)

        # Use the serializer to validate & create (handles file upload, amount mapping, due_date calc)
        serializer = CustomerInvoiceSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            invoice = serializer.save()
            return Response({'success': True, 'invoice': CustomerInvoiceSerializer(invoice).data})
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

class ReconciliationConfirmView(APIView):
    """Confirm and save reconciliation results"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request, *args, **kwargs):
        """Save confirmed reconciliation and create payment advice"""
        tenant = get_current_tenant()
        if not tenant:
            return Response({'error': 'No tenant context'}, status=400)
        
        customer_id = request.data.get('customer_id')
        advice_number = request.data.get('advice_number')
        advice_date_str = request.data.get('advice_date')
        total_amount_str = request.data.get('total_amount')
        matched_invoices = request.data.get('matched_invoices', [])
        notes = request.data.get('notes', '')
        
        if not all([customer_id, advice_number, advice_date_str, total_amount_str]):
            return Response({'error': 'Missing required fields'}, status=400)
        
        try:
            customer = Party.objects.get(id=customer_id, tenant=tenant, party_type='customer')
            
            # Parse date
            try:
                advice_date = datetime.strptime(advice_date_str, '%Y-%m-%d').date()
            except (ValueError, TypeError) as e:
                return Response({
                    'error': f'Invalid date format: {advice_date_str}. Expected YYYY-MM-DD'
                }, status=400)
            
            # Parse amount
            try:
                total_amount = Decimal(str(total_amount_str).replace(',', ''))
            except (ValueError, TypeError) as e:
                return Response({
                    'error': f'Invalid amount: {total_amount_str}'
                }, status=400)
            
            reconciliation_service = ReconciliationService(tenant)
            
            # Prepare invoice data
            matched_invoice_ids = [inv['invoice_id'] for inv in matched_invoices]
            invoice_amounts = {}
            
            for inv in matched_invoices:
                invoice_id = inv['invoice_id']
                amount_str = inv.get('amount_in_advice') or inv.get('invoice_amount')
                
                try:
                    invoice_amounts[invoice_id] = Decimal(str(amount_str).replace(',', ''))
                except (ValueError, TypeError):
                    # Use invoice's actual amount as fallback
                    try:
                        invoice = CustomerInvoice.objects.get(id=invoice_id, tenant=tenant)
                        invoice_amounts[invoice_id] = invoice.invoice_amount
                    except:
                        continue
            
            # Create payment advice with reconciliation
            payment_advice, reconciliation_summary = reconciliation_service.create_payment_advice_with_reconciliation(
                customer=customer,
                advice_number=advice_number,
                advice_date=advice_date,
                total_payment_amount=total_amount,
                matched_invoice_ids=matched_invoice_ids,
                invoice_amounts=invoice_amounts,
                created_by=request.user,
                notes=notes
            )
            
            # Handle document upload if provided
            if 'document' in request.FILES:
                payment_advice.advice_document = request.FILES['document']
                payment_advice.save()
            
            return Response({
                'success': True,
                'payment_advice_id': payment_advice.id,
                'advice_number': payment_advice.advice_number,
                'reconciliation_summary': reconciliation_summary,
                'message': 'Reconciliation confirmed and payment advice created'
            })
            
        except Party.DoesNotExist:
            return Response({'error': 'Customer not found'}, status=404)
        except Exception as e:
            logger.error(f"Reconciliation confirmation failed: {e}", exc_info=True)
            return Response({
                'success': False,
                'error': f'Confirmation failed: {str(e)}'
            }, status=500)


@api_view(['GET'])
def customer_unpaid_invoices(request, customer_id):
    """Get unpaid invoices for a customer for manual reconciliation"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    try:
        customer = Party.objects.get(id=customer_id, tenant=tenant, party_type='customer')
        
        reconciliation_service = ReconciliationService(tenant)
        unpaid_invoices = reconciliation_service.get_unpaid_invoices(customer)
        
        invoice_data = []
        for invoice in unpaid_invoices:
            # Calculate days overdue safely
            days_overdue = 0
            if invoice.due_date:
                days_overdue = (timezone.now().date() - invoice.due_date).days
            
            invoice_data.append({
                'id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'invoice_date': str(invoice.invoice_date),
                'due_date': str(invoice.due_date) if invoice.due_date else None,
                'amount': str(invoice.invoice_amount),
                'status': invoice.status,
                'days_overdue': max(0, days_overdue),
                'customer_name': customer.display_name
            })
        
        total_amount = sum(Decimal(inv['amount']) for inv in invoice_data)
        
        return Response({
            'customer': {
                'id': customer.id,
                'name': customer.display_name,
                'code': customer.party_code
            },
            'unpaid_invoices': invoice_data,
            'total_count': len(invoice_data),
            'total_amount': str(total_amount)
        })
        
    except Party.DoesNotExist:
        return Response({'error': 'Customer not found'}, status=404)
    except Exception as e:
        logger.error(f"Failed to get unpaid invoices: {e}", exc_info=True)
        return Response({
            'error': 'Failed to retrieve unpaid invoices'
        }, status=500)

# Add this to views.py

@api_view(['GET'])
def reconciliation_dashboard_data(request):
    """Get comprehensive data for reconciliation dashboard with filtering"""
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    # Get filter parameters
    customer_id = request.query_params.get('customer_id')
    start_date = request.query_params.get('start_date')
    end_date = request.query_params.get('end_date')
    status_filter = request.query_params.get('status')
    
    # Base queries
    invoices_query = CustomerInvoice.objects.filter(tenant=tenant, is_active=True)
    payment_advices_query = PaymentAdvice.objects.filter(tenant=tenant, is_active=True)
    customer_pos_query = CustomerPurchaseOrder.objects.filter(tenant=tenant, is_active=True)
    
    # Apply filters
    if customer_id:
        invoices_query = invoices_query.filter(customer_id=customer_id)
        payment_advices_query = payment_advices_query.filter(customer_id=customer_id)
        customer_pos_query = customer_pos_query.filter(customer_id=customer_id)
    
    if start_date:
        invoices_query = invoices_query.filter(invoice_date__gte=start_date)
        payment_advices_query = payment_advices_query.filter(advice_date__gte=start_date)
        customer_pos_query = customer_pos_query.filter(po_date__gte=start_date)
    
    if end_date:
        invoices_query = invoices_query.filter(invoice_date__lte=end_date)
        payment_advices_query = payment_advices_query.filter(advice_date__lte=end_date)
        customer_pos_query = customer_pos_query.filter(po_date__lte=end_date)
    
    if status_filter:
        invoices_query = invoices_query.filter(status=status_filter)
    
    # Get data with relationships
    invoices = invoices_query.select_related('customer', 'reference_customer_po').order_by('-invoice_date')
    payment_advices = payment_advices_query.select_related('customer').prefetch_related('mentioned_invoices').order_by('-advice_date')
    customer_pos = customer_pos_query.select_related('customer').order_by('-po_date')
    
    # Serialize data
    invoice_data = []
    for inv in invoices:
        # Get related payment advices
        related_payments = PaymentAdviceInvoice.objects.filter(invoice=inv).select_related('payment_advice')
        payments_info = [{
            'advice_number': pa.payment_advice.advice_number,
            'advice_date': str(pa.payment_advice.advice_date),
            'amount': str(pa.amount_mentioned)
        } for pa in related_payments]
        
        invoice_data.append({
            'id': inv.id,
            'invoice_number': inv.invoice_number,
            'customer_name': inv.customer.display_name,
            'customer_id': inv.customer.id,
            'invoice_date': str(inv.invoice_date),
            'due_date': str(inv.due_date) if inv.due_date else None,
            'amount': str(inv.invoice_amount),
            'status': inv.status,
            'customer_po_number': inv.reference_customer_po.po_number if inv.reference_customer_po else None,
            'document_url': inv.invoice_document.url if inv.invoice_document else None,
            'related_payments': payments_info,
            'total_paid': str(sum(Decimal(p['amount']) for p in payments_info)),
            'balance': str(inv.invoice_amount - sum(Decimal(p['amount']) for p in payments_info))
        })
    
    payment_advice_data = []
    for pa in payment_advices:
        linked_invoices = PaymentAdviceInvoice.objects.filter(payment_advice=pa).select_related('invoice')
        invoices_info = [{
            'invoice_number': pai.invoice.invoice_number,
            'invoice_date': str(pai.invoice.invoice_date),
            'amount': str(pai.amount_mentioned)
        } for pai in linked_invoices]
        
        payment_advice_data.append({
            'id': pa.id,
            'advice_number': pa.advice_number,
            'customer_name': pa.customer.display_name,
            'customer_id': pa.customer.id,
            'advice_date': str(pa.advice_date),
            'total_amount': str(pa.total_payment_amount),
            'document_url': pa.advice_document.url if pa.advice_document else None,
            'linked_invoices': invoices_info,
            'total_allocated': str(sum(Decimal(i['amount']) for i in invoices_info)),
            'unallocated': str(pa.total_payment_amount - sum(Decimal(i['amount']) for i in invoices_info)),
            'notes': pa.notes
        })
    
    customer_po_data = []
    for cpo in customer_pos:
        related_invoices = CustomerInvoice.objects.filter(reference_customer_po=cpo)
        invoices_info = [{
            'invoice_number': inv.invoice_number,
            'amount': str(inv.invoice_amount),
            'status': inv.status
        } for inv in related_invoices]
        
        customer_po_data.append({
            'id': cpo.id,
            'po_number': cpo.po_number,
            'customer_name': cpo.customer.display_name,
            'customer_id': cpo.customer.id,
            'po_date': str(cpo.po_date),
            'po_amount': str(cpo.po_amount),
            'status': cpo.status,
            'document_url': cpo.po_document.url if cpo.po_document else None,
            'related_invoices': invoices_info,
            'total_invoiced': str(sum(Decimal(i['amount']) for i in invoices_info))
        })
    
    # Calculate summary statistics
    total_invoices = len(invoice_data)
    total_invoice_amount = sum(Decimal(inv['amount']) for inv in invoice_data)
    total_paid_amount = sum(Decimal(inv['total_paid']) for inv in invoice_data)
    total_outstanding = total_invoice_amount - total_paid_amount
    
    return Response({
        'invoices': invoice_data,
        'payment_advices': payment_advice_data,
        'customer_pos': customer_po_data,
        'summary': {
            'total_invoices': total_invoices,
            'total_invoice_amount': str(total_invoice_amount),
            'total_paid': str(total_paid_amount),
            'total_outstanding': str(total_outstanding),
            'total_payment_advices': len(payment_advice_data),
            'total_customer_pos': len(customer_po_data)
        }
    })


@api_view(['POST'])
def reconcile_invoice_numbers(request):
    """
    Reconcile manually entered invoice numbers with system invoices
    Supports date range filtering for invoice lookup
    """
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    customer_id = request.data.get('customer_id')
    invoice_entries = request.data.get('invoice_entries', [])  # [{"invoice_number": "INV-001", "amount": "5000"}, ...]
    date_range_days = request.data.get('date_range_days', 180)
    start_date = request.data.get('start_date')  # Optional custom start date
    end_date = request.data.get('end_date')  # Optional custom end date
    
    if not customer_id or not invoice_entries:
        return Response({
            'error': 'customer_id and invoice_entries are required'
        }, status=400)
    
    try:
        customer = Party.objects.get(id=customer_id, tenant=tenant, party_type='customer')
        
        # Build date filter
        if end_date:
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        else:
            end_date = timezone.now().date()
        
        if start_date:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        else:
            start_date = end_date - timedelta(days=date_range_days)
        
        # Get system invoices in date range
        system_invoices = CustomerInvoice.objects.filter(
            tenant=tenant,
            customer=customer,
            is_active=True,
            invoice_date__gte=start_date,
            invoice_date__lte=end_date,
            status__in=['sent', 'partial_paid', 'overdue']
        ).order_by('invoice_date')
        
        reconciliation_service = ReconciliationService(tenant)
        
        # Process each entered invoice
        matched_invoices = []
        unmatched_entries = []
        
        for entry in invoice_entries:
            invoice_number = entry.get('invoice_number', '').strip()
            amount = entry.get('amount', '')
            
            if not invoice_number:
                continue
            
            # Try to find matching invoice
            matched_invoice = reconciliation_service.fuzzy_match_invoice_number(
                invoice_number, 
                list(system_invoices)
            )
            
            if matched_invoice:
                # Calculate amount discrepancy if amount provided
                amount_discrepancy = None
                if amount:
                    try:
                        entered_amount = Decimal(str(amount).replace(',', ''))
                        invoice_amount = matched_invoice.invoice_amount
                        
                        if abs(entered_amount - invoice_amount) > Decimal('0.01'):
                            amount_discrepancy = {
                                'invoice_amount': str(invoice_amount),
                                'entered_amount': str(entered_amount),
                                'difference': str(invoice_amount - entered_amount)
                            }
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Invalid amount format: {amount}")
                
                days_overdue = 0
                if matched_invoice.due_date:
                    days_overdue = (timezone.now().date() - matched_invoice.due_date).days
                
                matched_invoices.append({
                    'invoice_id': matched_invoice.id,
                    'invoice_number': matched_invoice.invoice_number,
                    'entered_number': invoice_number,
                    'invoice_date': str(matched_invoice.invoice_date),
                    'due_date': str(matched_invoice.due_date) if matched_invoice.due_date else None,
                    'invoice_amount': str(matched_invoice.invoice_amount),
                    'entered_amount': amount,
                    'amount_discrepancy': amount_discrepancy,
                    'status': matched_invoice.status,
                    'days_overdue': max(0, days_overdue),
                    'match_quality': 'exact' if reconciliation_service.normalize_invoice_number(invoice_number) == 
                                     reconciliation_service.normalize_invoice_number(matched_invoice.invoice_number) 
                                     else 'fuzzy'
                })
            else:
                unmatched_entries.append({
                    'entered_number': invoice_number,
                    'entered_amount': amount,
                    'reason': 'No matching invoice found in system'
                })
        
        # Find missing invoices (in system but not in entered list)
        matched_invoice_ids = {m['invoice_id'] for m in matched_invoices}
        missing_invoices = []
        
        for invoice in system_invoices:
            if invoice.id not in matched_invoice_ids:
                days_overdue = 0
                if invoice.due_date:
                    days_overdue = (timezone.now().date() - invoice.due_date).days
                
                missing_invoices.append({
                    'invoice_id': invoice.id,
                    'invoice_number': invoice.invoice_number,
                    'invoice_date': str(invoice.invoice_date),
                    'due_date': str(invoice.due_date) if invoice.due_date else None,
                    'invoice_amount': str(invoice.invoice_amount),
                    'status': invoice.status,
                    'days_overdue': max(0, days_overdue),
                    'aging_bucket': reconciliation_service._get_aging_bucket(days_overdue)
                })
        
        # Calculate totals
        total_matched_amount = sum(Decimal(m['invoice_amount']) for m in matched_invoices)
        total_missing_amount = sum(Decimal(m['invoice_amount']) for m in missing_invoices)
        total_entered_amount = sum(
            Decimal(str(m['entered_amount']).replace(',', '')) 
            for m in matched_invoices if m['entered_amount']
        )
        
        # Generate recommendations
        recommendations = []
        
        if missing_invoices:
            recommendations.append({
                'type': 'warning',
                'message': f'{len(missing_invoices)} invoices (₹{total_missing_amount:,.2f}) missing from payment advice',
                'action': 'Contact customer accounts department'
            })
        
        if unmatched_entries:
            recommendations.append({
                'type': 'error',
                'message': f'{len(unmatched_entries)} invoice numbers not found in system',
                'action': 'Verify invoice numbers with customer'
            })
        
        discrepancy_count = sum(1 for m in matched_invoices if m.get('amount_discrepancy'))
        if discrepancy_count:
            recommendations.append({
                'type': 'warning',
                'message': f'{discrepancy_count} invoices have amount discrepancies',
                'action': 'Review and confirm amounts with customer'
            })
        
        critical_overdue = [m for m in missing_invoices if m['days_overdue'] > 90]
        if critical_overdue:
            recommendations.append({
                'type': 'critical',
                'message': f'{len(critical_overdue)} missing invoices are 90+ days overdue',
                'action': 'Escalate immediately'
            })
        
        if not recommendations:
            recommendations.append({
                'type': 'success',
                'message': 'All invoices reconciled successfully',
                'action': 'No action required'
            })
        
        return Response({
            'success': True,
            'reconciliation': {
                'date_range': {
                    'start_date': str(start_date),
                    'end_date': str(end_date)
                },
                'customer': {
                    'id': customer.id,
                    'name': customer.display_name,
                    'code': customer.party_code
                },
                'summary': {
                    'total_system_invoices': system_invoices.count(),
                    'total_entered': len(invoice_entries),
                    'total_matched': len(matched_invoices),
                    'total_missing': len(missing_invoices),
                    'total_unmatched': len(unmatched_entries),
                    'matched_amount': str(total_matched_amount),
                    'missing_amount': str(total_missing_amount),
                    'entered_amount': str(total_entered_amount)
                },
                'matched_invoices': matched_invoices,
                'missing_invoices': missing_invoices,
                'unmatched_entries': unmatched_entries,
                'recommendations': recommendations
            }
        })
        
    except Party.DoesNotExist:
        return Response({'error': 'Customer not found'}, status=404)
    except Exception as e:
        logger.error(f"Reconciliation failed: {e}", exc_info=True)
        return Response({
            'success': False,
            'error': str(e)
        }, status=500)


@api_view(['POST'])
def save_reconciliation(request):
    """
    Save confirmed reconciliation and create payment advice
    """
    tenant = get_current_tenant()
    if not tenant:
        return Response({'error': 'No tenant context'}, status=400)
    
    customer_id = request.data.get('customer_id')
    advice_number = request.data.get('advice_number')
    advice_date = request.data.get('advice_date')
    total_amount = request.data.get('total_amount')
    matched_invoices = request.data.get('matched_invoices', [])
    notes = request.data.get('notes', '')
    
    if not all([customer_id, advice_date, total_amount]):
        return Response({'error': 'Missing required fields'}, status=400)
    
    try:
        customer = Party.objects.get(id=customer_id, tenant=tenant, party_type='customer')
        
        # Auto-generate advice number if not provided
        if not advice_number:
            last_advice = PaymentAdvice.objects.filter(tenant=tenant).order_by('-id').first()
            advice_number = f"PA-{timezone.now().strftime('%Y%m')}-{(last_advice.id + 1) if last_advice else 1:04d}"
        
        # Parse date and amount
        advice_date = datetime.strptime(advice_date, '%Y-%m-%d').date()
        total_amount = Decimal(str(total_amount).replace(',', ''))
        
        # Prepare invoice data
        matched_invoice_ids = [inv['invoice_id'] for inv in matched_invoices]
        invoice_amounts = {}
        
        for inv in matched_invoices:
            invoice_id = inv['invoice_id']
            amount = inv.get('entered_amount') or inv.get('invoice_amount')
            try:
                invoice_amounts[invoice_id] = Decimal(str(amount).replace(',', ''))
            except:
                # Use invoice's actual amount as fallback
                invoice = CustomerInvoice.objects.get(id=invoice_id, tenant=tenant)
                invoice_amounts[invoice_id] = invoice.invoice_amount
        
        # Create payment advice
        reconciliation_service = ReconciliationService(tenant)
        payment_advice, summary = reconciliation_service.create_payment_advice_with_reconciliation(
            customer=customer,
            advice_number=advice_number,
            advice_date=advice_date,
            total_payment_amount=total_amount,
            matched_invoice_ids=matched_invoice_ids,
            invoice_amounts=invoice_amounts,
            created_by=request.user,
            notes=notes
        )
        
        return Response({
            'success': True,
            'payment_advice_id': payment_advice.id,
            'advice_number': payment_advice.advice_number,
            'message': 'Reconciliation saved successfully'
        })
        
    except Exception as e:
        logger.error(f"Save reconciliation failed: {e}", exc_info=True)
        return Response({
            'success': False,
            'error': str(e)
        }, status=500)