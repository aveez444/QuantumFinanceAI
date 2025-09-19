# core/ai_orchestrator.py
import json
import logging
from django.db.models import Sum, F
from django.utils import timezone
from rapidfuzz import fuzz, process


from .models import Product, StockMovement, WorkOrder, ProductionEntry, Employee, GLJournalLine, ProductionEntry
from .utils import detect_production_anomalies, generate_reorder_suggestions
from .llm_utils import call_llm

logger = logging.getLogger(__name__)

class AIOrchestrator:
    def __init__(self, tenant, user):
        self.tenant = tenant
        self.user = user

    def process_query(self, query: str) -> dict:
        """Main AI processing pipeline."""
        try:
            # Step 1: Classify intent
            classification = self._classify_intent(query)
            logger.info(f"AIOrchestrator intent: {classification}")

            # Step 2: Execute based on intent
            if classification == "db_query":
                system_data = self._execute_db_query(query)
            elif classification == "api_call":
                system_data = self._execute_api_call(query)
            elif classification == "hybrid":
                system_data = self._execute_hybrid(query)
            else:  # general
                system_data = {}

            # Step 3: Generate final response
            response = self._generate_response(query, system_data, classification)

            return {
                "success": True,
                "intent": classification,
                "system_data": system_data,
                "response": response,
                "metadata": {"tenant": self.tenant.id, "user": self.user.id, "time": timezone.now().isoformat()}
            }

        except Exception as e:
            logger.error(f"AIOrchestrator error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _classify_intent(self, query: str) -> str:
        """LLM + rules to classify query intent."""
        # Basic rules first
        if "product" in query.lower() or "inventory" in query.lower():
            return "db_query"
        if "reorder" in query.lower() or "anomaly" in query.lower():
            return "api_call"

        # Fallback to LLM
        intent_prompt = f"""
        Analyze this query: "{query}".
        Classify into one of: db_query, api_call, hybrid, general.
        """
        result = call_llm(intent_prompt)
        if isinstance(result, dict) and "classification" in result:
            return result["classification"]
        return "general"

    def _matches(self, query: str, keywords: list, threshold: int = 80) -> bool:
            return any(fuzz.partial_ratio(query, k) >= threshold for k in keywords)

    def _execute_db_query(self, query: str) -> dict:
        """Translate simple queries into ORM lookups."""
        q_lower = query.lower()

        # --- Production (check this before product) ---
        if self._matches(q_lower, ["production", "prod", "work order", "manufacturing"]):
            total_output = ProductionEntry.objects.filter(tenant=self.tenant).aggregate(
                total_produced=Sum("quantity_produced"),
                total_rejected=Sum("quantity_rejected"),
                total_downtime=Sum("downtime_minutes")
            )
            return {
                "production_summary": {
                    "produced": total_output["total_produced"] or 0,
                    "rejected": total_output["total_rejected"] or 0,
                    "downtime_minutes": total_output["total_downtime"] or 0,
                }
            }


        # --- Products (after production to avoid conflict) ---
        if self._matches(q_lower, ["product", "products", "item", "goods"]):

            products = Product.objects.filter(tenant=self.tenant, is_active=True)
            return {"products": [{"sku": p.sku, "name": p.product_name} for p in products]}

        if "employee" in q_lower or "staff" in q_lower or "people" in q_lower:
            employees = Employee.objects.filter(tenant=self.tenant, is_active=True)
            return {
                "employees": [
                    {"code": e.employee_code, "name": e.full_name, "department": e.department}
                    for e in employees
                ]
            }

        # --- Stock ---
        if self._matches(q_lower, ["stock", "inventory", "materials"]):

            stock = StockMovement.objects.filter(tenant=self.tenant).values(
                "product__sku", "product__product_name"
            ).annotate(current_stock=Sum("quantity"))
            return {"stock": list(stock)}

        # --- Financial ---
        if self._matches(q_lower, ["financial", "finance", "profit", "loss", "fin"]):

            from .models import GLJournalLine
            gl_query = GLJournalLine.objects.filter(
                tenant=self.tenant,
                journal__status="posted"
            )
            revenue = gl_query.filter(account__account_type="revenue").aggregate(
                total=Sum(F("credit_amount") - F("debit_amount"))
            )["total"] or 0
            cogs = gl_query.filter(account__account_type="cogs").aggregate(
                total=Sum(F("debit_amount") - F("credit_amount"))
            )["total"] or 0
            expenses = gl_query.filter(account__account_type="expense").aggregate(
                total=Sum(F("debit_amount") - F("credit_amount"))
            )["total"] or 0
            gross_profit = revenue - cogs
            net_profit = gross_profit - expenses
            return {
                "financial_summary": {
                    "revenue": float(revenue),
                    "cogs": float(cogs),
                    "expenses": float(expenses),
                    "gross_profit": float(gross_profit),
                    "net_profit": float(net_profit)
                }
            }

        # --- Analytics ---
        if self._matches(q_lower, ["analytics", "dashboard", "kpi", "report"]):

            from .utils import detect_production_anomalies, generate_reorder_suggestions, get_production_efficiency_trends
            return {
                "analytics": {
                    "anomalies": detect_production_anomalies(self.tenant, lookback_days=7),
                    "reorder_suggestions": generate_reorder_suggestions(self.tenant),
                    "efficiency_trends": get_production_efficiency_trends(self.tenant, days=30)
                }
            }

        return {"message": "No matching DB query found."}


    def _execute_api_call(self, query: str) -> dict:
        """Map to internal APIs."""
        if "reorder" in query.lower():
            return {"reorder_suggestions": generate_reorder_suggestions(self.tenant)}
        if "anomaly" in query.lower():
            return {"anomalies": detect_production_anomalies(self.tenant)}
        return {"message": "No matching API found."}

    def _execute_hybrid(self, query: str) -> dict:
        """Combine DB/API results + LLM reasoning."""
        data = self._execute_db_query(query)
        explanation = call_llm(f"User asked: {query}. Data: {data}. Explain with insights.")
        return {"data": data, "insights": explanation}

    def _generate_response(self, query: str, data: dict, classification: str) -> str:
        """Format output intelligently."""
        if classification in ["db_query", "api_call", "hybrid"]:
            # Custom pretty format for employees
            if "employees" in data:
                employees_list = data["employees"]
                if not employees_list:
                    return f"📊 Based on your query '{query}', no employees found."

                response = f"📊 Based on your query '{query}', here are your employees:\n"
                for emp in employees_list:
                    response += f"- {emp['code']} | {emp['name']} ({emp['department']})\n"
                return response

            # Default: JSON dump
            return f"📊 Based on your query '{query}', here’s what I found:\n{json.dumps(data, indent=2, default=str)}"
        else:
            return call_llm(f"Answer this general query: {query}")
