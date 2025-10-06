# core/reconciliation_service.py - Payment Advice Reconciliation Service (Completed)

import logging
from typing import Dict, List, Any, Optional, Tuple
from decimal import Decimal
from datetime import datetime, timedelta
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    CustomerInvoice, PaymentAdvice, PaymentAdviceInvoice, 
    Party, Tenant
)

logger = logging.getLogger(__name__)

class ReconciliationService:
    """
    Handles payment advice reconciliation with customer invoices
    Identifies missing invoices and discrepancies
    """
    
    def __init__(self, tenant: Tenant):
        self.tenant = tenant
    
    def get_unpaid_invoices(
        self, 
        customer: Party, 
        date_range_days: int = 180,
        as_of_date: Optional[datetime.date] = None
    ) -> List[CustomerInvoice]:
        """
        Get all unpaid/partially paid invoices for a customer
        """
        if as_of_date is None:
            as_of_date = timezone.now().date()
        
        start_date = as_of_date - timedelta(days=date_range_days)
        
        unpaid_invoices = CustomerInvoice.objects.filter(
            tenant=self.tenant,
            customer=customer,
            is_active=True,
            invoice_date__gte=start_date,
            invoice_date__lte=as_of_date,
            status__in=['sent', 'partial_paid', 'overdue']
        ).order_by('invoice_date')
        
        return list(unpaid_invoices)
    
    def normalize_invoice_number(self, invoice_number: str) -> str:
        """
        Normalize invoice number for matching
        """
        normalized = invoice_number.upper().strip()
        normalized = normalized.replace(' ', '')
        normalized = normalized.replace('/', '-').replace('_', '-')
        return normalized
    
    def fuzzy_match_invoice_number(
        self, 
        extracted_number: str, 
        system_invoices: List[CustomerInvoice]
    ) -> Optional[CustomerInvoice]:
        """
        Try to match extracted invoice number with system invoices
        """
        normalized_extracted = self.normalize_invoice_number(extracted_number)
        
        for invoice in system_invoices:
            normalized_system = self.normalize_invoice_number(invoice.invoice_number)
            
            if normalized_extracted == normalized_system:
                return invoice
            
            if normalized_extracted in normalized_system or normalized_system in normalized_extracted:
                return invoice
        
        return None
    
    def reconcile_ocr_data(
        self, 
        customer: Party,
        extracted_invoice_numbers: List[str],
        extracted_amounts: Dict[str, str] = None,
        date_range_days: int = 180,
        payment_advice_date: Optional[datetime.date] = None
    ) -> Dict[str, Any]:
        """
        Reconcile OCR-extracted payment advice data with system invoices
        """
        if extracted_amounts is None:
            extracted_amounts = {}
        
        system_invoices = self.get_unpaid_invoices(
            customer, 
            date_range_days, 
            payment_advice_date
        )
        
        matched_invoices = []
        unmatched_extracted = []
        
        for extracted_num in extracted_invoice_numbers:
            matched_invoice = self.fuzzy_match_invoice_number(extracted_num, system_invoices)
            
            if matched_invoice:
                amount_in_advice = extracted_amounts.get(extracted_num)
                amount_discrepancy = None
                
                if amount_in_advice:
                    try:
                        advice_amount = Decimal(amount_in_advice)
                        invoice_amount = matched_invoice.invoice_amount
                        
                        if abs(advice_amount - invoice_amount) > Decimal('0.01'):
                            amount_discrepancy = {
                                'invoice_amount': str(invoice_amount),
                                'advice_amount': str(advice_amount),
                                'difference': str(invoice_amount - advice_amount)
                            }
                    except:
                        pass
                
                matched_invoices.append({
                    'invoice_id': matched_invoice.id,
                    'invoice_number': matched_invoice.invoice_number,
                    'extracted_number': extracted_num,
                    'invoice_date': matched_invoice.invoice_date,
                    'invoice_amount': str(matched_invoice.invoice_amount),
                    'amount_in_advice': amount_in_advice,
                    'amount_discrepancy': amount_discrepancy,
                    'status': matched_invoice.status,
                    'match_quality': 'exact' if self.normalize_invoice_number(extracted_num) == 
                                     self.normalize_invoice_number(matched_invoice.invoice_number) 
                                     else 'fuzzy'
                })
            else:
                unmatched_extracted.append({
                    'extracted_number': extracted_num,
                    'amount': extracted_amounts.get(extracted_num),
                    'reason': 'No matching invoice found in system'
                })
        
        matched_invoice_ids = {m['invoice_id'] for m in matched_invoices}
        missing_invoices = []
        
        for invoice in system_invoices:
            if invoice.id not in matched_invoice_ids:
                days_overdue = (timezone.now().date() - invoice.due_date).days if invoice.due_date else 0
                
                missing_invoices.append({
                    'invoice_id': invoice.id,
                    'invoice_number': invoice.invoice_number,
                    'invoice_date': invoice.invoice_date,
                    'due_date': invoice.due_date,
                    'invoice_amount': str(invoice.invoice_amount),
                    'status': invoice.status,
                    'days_overdue': max(0, days_overdue),
                    'aging_bucket': self._get_aging_bucket(days_overdue)
                })
        
        total_matched_amount = sum(
            Decimal(m['invoice_amount']) for m in matched_invoices
        )
        total_missing_amount = sum(
            Decimal(m['invoice_amount']) for m in missing_invoices
        )
        
        return {
            'reconciliation_date': timezone.now().date(),
            'customer': {
                'id': customer.id,
                'code': customer.party_code,
                'name': customer.display_name
            },
            'summary': {
                'total_system_invoices': len(system_invoices),
                'total_matched': len(matched_invoices),
                'total_missing': len(missing_invoices),
                'total_unmatched_extracted': len(unmatched_extracted),
                'matched_amount': str(total_matched_amount),
                'missing_amount': str(total_missing_amount)
            },
            'matched_invoices': matched_invoices,
            'missing_invoices': missing_invoices,
            'unmatched_extracted': unmatched_extracted,
            'recommendations': self._generate_recommendations(
                matched_invoices, 
                missing_invoices, 
                unmatched_extracted
            )
        }
    
    def reconcile_manual_data(
        self,
        customer: Party,
        manual_invoice_numbers: List[str],
        date_range_days: int = 180
    ) -> Dict[str, Any]:
        """
        Reconcile manually entered invoice numbers
        """
        system_invoices = self.get_unpaid_invoices(customer, date_range_days)
        system_invoice_map = {
            self.normalize_invoice_number(inv.invoice_number): inv 
            for inv in system_invoices
        }
        
        matched_invoices = []
        unmatched_manual = []
        
        for manual_num in manual_invoice_numbers:
            normalized = self.normalize_invoice_number(manual_num)
            invoice = system_invoice_map.get(normalized)
            
            if invoice:
                matched_invoices.append({
                    'invoice_id': invoice.id,
                    'invoice_number': invoice.invoice_number,
                    'entered_number': manual_num,
                    'invoice_date': invoice.invoice_date,
                    'invoice_amount': str(invoice.invoice_amount),
                    'status': invoice.status
                })
            else:
                unmatched_manual.append({
                    'entered_number': manual_num,
                    'reason': 'Invoice not found or already paid'
                })
        
        matched_invoice_ids = {m['invoice_id'] for m in matched_invoices}
        missing_invoices = []
        
        for invoice in system_invoices:
            if invoice.id not in matched_invoice_ids:
                days_overdue = (timezone.now().date() - invoice.due_date).days if invoice.due_date else 0
                missing_invoices.append({
                    'invoice_id': invoice.id,
                    'invoice_number': invoice.invoice_number,
                    'invoice_date': invoice.invoice_date,
                    'due_date': invoice.due_date,
                    'invoice_amount': str(invoice.invoice_amount),
                    'status': invoice.status,
                    'days_overdue': max(0, days_overdue)
                })
        
        total_matched = sum(Decimal(m['invoice_amount']) for m in matched_invoices)
        total_missing = sum(Decimal(m['invoice_amount']) for m in missing_invoices)
        
        return {
            'reconciliation_date': timezone.now().date(),
            'customer': {
                'id': customer.id,
                'code': customer.party_code,
                'name': customer.display_name
            },
            'summary': {
                'total_system_invoices': len(system_invoices),
                'total_matched': len(matched_invoices),
                'total_missing': len(missing_invoices),
                'total_unmatched_manual': len(unmatched_manual),
                'matched_amount': str(total_matched),
                'missing_amount': str(total_missing)
            },
            'matched_invoices': matched_invoices,
            'missing_invoices': missing_invoices,
            'unmatched_manual': unmatched_manual,
            'recommendations': self._generate_recommendations(
                matched_invoices,
                missing_invoices,
                unmatched_manual
            )
        }
    
    def _get_aging_bucket(self, days_overdue: int) -> str:
        """Categorize invoice by aging"""
        if days_overdue <= 0:
            return 'current'
        elif days_overdue <= 30:
            return '1-30 days'
        elif days_overdue <= 60:
            return '31-60 days'
        elif days_overdue <= 90:
            return '61-90 days'
        else:
            return '90+ days'
    
    def _generate_recommendations(
        self,
        matched: List[Dict],
        missing: List[Dict],
        unmatched: List[Dict]
    ) -> List[str]:
        """Generate actionable recommendations based on reconciliation"""
        recommendations = []
        
        if len(missing) > 0:
            total_missing = sum(Decimal(m['invoice_amount']) for m in missing)
            recommendations.append(
                f"âš ï¸ {len(missing)} invoices (â‚¹{total_missing:,.2f}) are missing from payment advice. "
                f"Contact customer's accounts department."
            )
        
        if len(unmatched) > 0:
            recommendations.append(
                f"â“ {len(unmatched)} invoice numbers in payment advice not found in system. "
                f"Verify these invoice numbers with customer."
            )
        
        discrepancies = [m for m in matched if m.get('amount_discrepancy')]
        if discrepancies:
            recommendations.append(
                f"ðŸ’° {len(discrepancies)} invoices have amount discrepancies. "
                f"Review and confirm correct amounts with customer."
            )
        
        critical_overdue = [m for m in missing if m.get('days_overdue', 0) > 90]
        if critical_overdue:
            recommendations.append(
                f"ðŸš¨ {len(critical_overdue)} missing invoices are 90+ days overdue. "
                f"Escalate for immediate follow-up."
            )
        
        if not recommendations:
            recommendations.append(
                "âœ… All invoices reconciled successfully. No action required."
            )
        
        return recommendations
    
    def create_payment_advice_with_reconciliation(
        self,
        customer: Party,
        advice_number: str,
        advice_date: datetime.date,
        total_payment_amount: Decimal,
        matched_invoice_ids: List[int],
        invoice_amounts: Dict[int, Decimal],
        created_by,
        notes: str = ""
    ) -> Tuple[PaymentAdvice, Dict[str, Any]]:
        """
        Create payment advice record and link matched invoices
        """
        from django.db import transaction
        
        with transaction.atomic():
            # Create payment advice
            payment_advice = PaymentAdvice.objects.create(
                tenant=self.tenant,
                customer=customer,
                advice_number=advice_number,
                advice_date=advice_date,
                total_payment_amount=total_payment_amount,
                notes=notes,
                created_by=created_by
            )
            
            # Link invoices
            linked_count = 0
            for invoice_id in matched_invoice_ids:
                try:
                    invoice = CustomerInvoice.objects.get(
                        id=invoice_id, 
                        tenant=self.tenant,
                        customer=customer
                    )
                    
                    amount_mentioned = invoice_amounts.get(invoice_id, invoice.invoice_amount)
                    
                    PaymentAdviceInvoice.objects.create(
                        tenant=self.tenant,
                        payment_advice=payment_advice,
                        invoice=invoice,
                        amount_mentioned=amount_mentioned,
                        created_by=created_by
                    )
                    
                    linked_count += 1
                    
                except CustomerInvoice.DoesNotExist:
                    logger.warning(f"Invoice {invoice_id} not found for customer {customer.id}")
            
            # Update invoice statuses
            self._update_invoice_statuses(matched_invoice_ids)
            
            reconciliation_summary = {
                'payment_advice_id': payment_advice.id,
                'advice_number': advice_number,
                'invoices_linked': linked_count,
                'total_amount': str(total_payment_amount),
                'created_at': timezone.now().isoformat()
            }
            
            return payment_advice, reconciliation_summary
    
    def _update_invoice_statuses(self, invoice_ids: List[int]):
        """Update status of matched invoices"""
        invoices = CustomerInvoice.objects.filter(
            id__in=invoice_ids,
            tenant=self.tenant
        )
        
        for invoice in invoices:
            if invoice.status == 'sent':
                invoice.status = 'partial_paid'
            elif invoice.status == 'partial_paid':
                # Check if fully paid (this would require more sophisticated logic)
                pass
            invoice.save()
            