# core/urls.py - ERP API URL Configuration

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CreateTenantView, LoginView, LogoutView, GetCSRFTokenView, PurchaseOrderViewSet
from . import views, business_views
from .business_views import *  # Import from business_views
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

# Create router for ViewSets
router = DefaultRouter()
router.register(r'products', views.ProductViewSet, basename='products')
router.register(r'work-orders', views.WorkOrderViewSet, basename='work-orders')
router.register(r'production-entries', views.ProductionEntryViewSet, basename='production-entries')
router.register(r'equipment', views.EquipmentViewSet, basename='equipment')
router.register(r'employees', views.EmployeeViewSet, basename='employees')
router.register(r'stock-movements', views.StockMovementViewSet, basename='stock-movements')
router.register(r'gl-journals', views.GLJournalViewSet, basename='gl-journals')
router.register(r'cost-centers', views.CostCenterViewSet, basename='cost-centers')
router.register(r'parties', views.PartyViewSet, basename='parties')
router.register(r'warehouses', views.WarehouseViewSet, basename='warehouses')
router.register(r'chart-of-accounts', views.ChartOfAccountsViewSet, basename='chart-of-accounts')
router.register(r'purchase-orders', PurchaseOrderViewSet)
router.register(r'employee-documents', views.EmployeeDocumentViewSet, basename='employee-documents')
router.register(r'customer-invoices', views.CustomerInvoiceViewSet, basename='customer-invoices')
router.register(r'customer-pos', views.CustomerPurchaseOrderViewSet, basename='customer-pos')
router.register(r'payment-advices', views.PaymentAdviceViewSet, basename='payment-advices')

urlpatterns = [
    path("create-tenant/", CreateTenantView.as_view(), name="create-tenant"),
    path("csrf/", GetCSRFTokenView.as_view(), name="csrf"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    path('auth/tenant-info/', views.tenant_info, name='tenant-info'),
    
    # Dashboard & Analytics
    path('dashboard/executive/', views.executive_dashboard, name='executive-dashboard'),
    path('dashboard/production/', views.production_efficiency_report, name='production-dashboard'),
    path('dashboard/kpis/', views.kpi_dashboard, name='kpi-dashboard'),
    
    # Quick Operations
    path('operations/stock-adjustment/', views.quick_stock_adjustment, name='stock-adjustment'),
    path('operations/stock-transfer/', views.StockMovementViewSet.as_view({'post': 'stock_transfer'}), name='stock-transfer'),
    
    # Data Management
    path('data/import-csv/', views.import_csv_data, name='import-csv'),
    path('data/export/', views.export_data, name='export-data'),
    
    # System Utilities
    path('system/health/', views.system_health, name='system-health'),
    path('system/audit-trail/', views.audit_trail, name='audit-trail'),

    path('ai-query/', views.AIQueryView.as_view(), name='ai_query'),
    path('ai-test/', views.ai_test_view, name='ai_test'),  # New test UI
        
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/token/blacklist/', LogoutView.as_view(), name='token_blacklist'),
    
    # Include ViewSet URLs
    path('api/', include(router.urls)),

    # OCR and Reconciliation endpoints
    path('reconcile/payment-advice/', views.PaymentAdviceReconcileView.as_view(), name='reconcile-payment-advice'),
    path('reconcile/confirm/', views.ReconciliationConfirmView.as_view(), name='reconcile-confirm'),
    path('customers/<int:customer_id>/unpaid-invoices/', views.customer_unpaid_invoices, name='customer-unpaid-invoices'),
    # In business_patterns or main urlpatterns
    path('reconciliation/dashboard-data/', views.reconciliation_dashboard_data, name='reconciliation-dashboard-data'),
    path('reconcile/invoice-numbers/', views.reconcile_invoice_numbers, name='reconcile-invoice-numbers'),
    path('save-reconciliation/', views.save_reconciliation, name='save-reconciliation'),


    # Add the warehouse-stock endpoint explicitly
    path('stock-movements/warehouse-stock/', views.StockMovementViewSet.as_view({'get': 'warehouse_stock'}), name='warehouse-stock'),
]

# Additional URL patterns for specific business processes
business_patterns = [
    # Production Planning
    path('planning/schedule-suggestions/', production_schedule_suggestions, name='schedule-suggestions'),
    path('planning/capacity-analysis/', capacity_analysis, name='capacity-analysis'),
    
    # Inventory Management
    path('inventory/reorder-suggestions/', reorder_suggestions, name='reorder-suggestions'),
    path('inventory/valuation/', inventory_valuation, name='inventory-valuation'),
    path('inventory/valuation/category/<str:category_name>/', category_valuation_detail, name='category-valuation-detail'),
    path('inventory/abc-analysis/', abc_analysis, name='abc-analysis'),
    
    # Financial Reports
    path('finance/trial-balance/', views.GLJournalViewSet.as_view({'get': 'trial_balance'}), name='trial-balance'),
    path('finance/profit-loss/', profit_loss_statement, name='profit-loss'),
    path('finance/cost-center-analysis/', cost_center_analysis, name='cost-center-analysis'),
    
    # Quality & Compliance
    path('quality/rejection-analysis/', rejection_analysis, name='rejection-analysis'),
    path('quality/oee-trends/', oee_trends, name='oee-trends'),

    # Material Consumption
    path('reports/material-consumption/<int:wo_id>/', material_consumption_report, name='material-consumption-report'),
    # Anomaly Detection
    path('quality/anomalies/', production_anomalies, name='production-anomalies'),
    # Financial Summary
    path('finance/summary/', financial_summary, name='financial-summary'),
    # Automated GL Entry
    path('finance/create-gl-entry/', create_gl_entry, name='create-gl-entry'),
    # Dashboard Alerts
    path('dashboard/alerts/', dashboard_alerts, name='dashboard-alerts'),

    path('business-overview/', business_views.business_overview_dashboard, name='business-overview-dashboard'),
    # In urls.py - business_patterns
    path('operations/overdue-work-orders/', overdue_work_orders, name='overdue-work-orders'),

]

urlpatterns.extend(business_patterns)
