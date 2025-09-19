# enhanced_ai_engine.py

from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
import re
from datetime import date, datetime, timedelta
import logging

from django.db.models import Sum, F, Q, Case, When, DecimalField
from django.utils.timezone import make_aware, get_current_timezone

from .llm_utils import call_llm
from .models import (
    Tenant, Product, Party, Employee, Equipment, WorkOrder,
    ProductionEntry, Warehouse, StockMovement,
    ChartOfAccounts, GLJournal, GLJournalLine
)

# -----------------------------
# ERPAIEngine: Simple. Smart. Strong.
# -----------------------------
class ERPAIEngine:
    """
    All-rounder AI business assistant for the ERP.
    - Interprets natural language into a structured intent
    - Executes safe, tenant-scoped ORM queries
    - Produces an auditable answer + the exact data used
    """

    def __init__(self, tenant: Tenant, user):
        self.tenant = tenant
        self.user = user
        self.tz = get_current_timezone()

    # ---------- Public entrypoint ----------

    def process_query(self, user_query: str) -> Dict[str, Any]:
        """
        1) Understand user query (LLM -> structured intent)
        2) Execute ORM according to intent
        3) Summarize answer + return raw data
        """
        # Step 1: Understand intent
        intent = self._llm_understand(user_query)

        # Fallback if LLM didn't return what we expected
        if not isinstance(intent, dict) or not intent.get("domain"):
            intent = self._default_backstop_intent(user_query)

        # Step 2: Execute intent via router
        try:
            data, summary = self._execute_intent(intent)
            interpretation = self._interpret_result(intent.get("domain"), summary, data)
            return {
                "success": True,
                "response": summary,
                "analysis": interpretation,
                "data": data,
                "meta": {"intent": intent}
            }
        except Exception as e:
            # Last-resort safe error surface
            return {
                "success": False,
                "error": str(e),
                "response": "I couldn’t complete that query. Try simplifying or changing the filters.",
                "meta": {"intent": intent}
            }

    # ---------- LLM “understanding” ----------

    def _llm_understand(self, user_query: str) -> Dict[str, Any]:
        """
        Ask LLM to translate NL -> intent JSON (domain, action, filters, dates, group_by, metrics).
        Stays lightweight and deterministic; the heavy lifting remains ORM-side.
        """
        system_hints = f"""
You are an ERP NL->Intent parser. Return STRICT JSON with keys:
- domain: one of ["products","inventory","work_orders","production","finance","parties","equipment","employees"]
- action: one of ["list","summary","detail","top_n","kpi"]
- filters: object with optional fields:
    product_type (one of raw_material, finished_good, semi_finished, consumable),
    category (string),
    status (for work orders: planned, released, in_progress, completed, cancelled),
    warehouse_code (string), cost_center_code (string),
    party_type (customer|supplier|other), account_type (asset|liability|equity|revenue|expense|cogs),
    text (free text search to apply on names/codes)
- date_range: object with any of:
    preset (one of today,yesterday,last_7_days,last_30_days,this_month,last_month,this_quarter,this_year),
    from (YYYY-MM-DD), to (YYYY-MM-DD)
- metrics: array of strings (e.g., ["current_stock","stock_value","quantity_completed","downtime_minutes"])
- group_by: array of strings (e.g., ["category","product_type","warehouse"])
- limit: integer (optional), default 100
- order_by: array of strings with optional '-' prefix (e.g., ["-current_stock","sku"])

STRICTLY avoid inventing columns. If the request mentions “Spare Part”, treat as filters.category="Spare Part".
If unclear, pick the most probable domain and set minimal filters, do not hallucinate.
"""
        prompt = f"""{system_hints}

USER_QUERY: {user_query}

Return JSON only."""
        intent = call_llm(prompt, response_format="json_object")
        if isinstance(intent, dict) and intent.get("error"):
            # If the LLM wrapper surfaced an error, fall back
            return {}
        return intent if isinstance(intent, dict) else {}

    def _default_backstop_intent(self, user_query: str) -> Dict[str, Any]:
        """
        Sensible default: try products listing with text filter.
        """
        return {
            "domain": "products",
            "action": "list",
            "filters": {"text": user_query[:80]},
            "limit": 100
        }

    # ---------- Intent execution router ----------

    def _execute_intent(self, intent: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        domain = intent.get("domain")
        action = intent.get("action", "list")
        filters = intent.get("filters", {}) or {}
        date_range = self._resolve_date_range(intent.get("date_range"))
        group_by = intent.get("group_by") or []
        metrics = intent.get("metrics") or []
        limit = int(intent.get("limit") or 100)
        order_by = intent.get("order_by") or []

        if domain == "products":
            return self._handle_products(action, filters, limit, order_by)
        if domain == "inventory":
            return self._handle_inventory(action, filters, date_range, group_by, metrics, limit, order_by)
        if domain == "work_orders":
            return self._handle_work_orders(action, filters, date_range, group_by, metrics, limit, order_by)
        if domain == "production":
            return self._handle_production(action, filters, date_range, group_by, metrics, limit, order_by)
        if domain == "finance":
            return self._handle_finance(action, filters, date_range, group_by, metrics, limit, order_by)
        if domain == "parties":
            return self._handle_parties(action, filters, limit, order_by)
        if domain == "equipment":
            return self._handle_equipment(action, filters, date_range, group_by, metrics, limit, order_by)


        if domain == "employees":
            return self._handle_employees(action, filters, limit, order_by)

        # Unknown domain -> default to product list
        return self._handle_products("list", filters, limit, order_by)

    # ---------- Helpers: date ranges, serialization, stock math ----------

    def _resolve_date_range(self, dr: Optional[Dict[str, Any]]) -> Optional[Tuple[datetime, datetime]]:
        """
        Convert presets or explicit strings into tz-aware (start, end) datetimes.
        Supports: today, yesterday, last_7_days, last_30_days, this_month, last_month, this_quarter, this_year
        Also supports explicit "from"/"to" in YYYY-MM-DD.
        """
        if not dr:
            return None

        now = datetime.now(self.tz)
        today = now.date()

        def mk_aware(d: date, end_of_day=False) -> datetime:
            dt = datetime(d.year, d.month, d.day, 23, 59, 59, 999999) if end_of_day else datetime(d.year, d.month, d.day)
            return make_aware(dt, self.tz)

        if "from" in dr and "to" in dr and self._is_ymd(dr["from"]) and self._is_ymd(dr["to"]):
            start = mk_aware(datetime.strptime(dr["from"], "%Y-%m-%d").date())
            end = mk_aware(datetime.strptime(dr["to"], "%Y-%m-%d").date(), end_of_day=True)
            return (start, end)

        preset = (dr.get("preset") or "").lower()
        if not preset:
            return None

        if preset == "today":
            return (mk_aware(today), mk_aware(today, True))
        if preset == "yesterday":
            y = today - timedelta(days=1)
            return (mk_aware(y), mk_aware(y, True))
        if preset == "last_7_days":
            start = today - timedelta(days=6)
            return (mk_aware(start), mk_aware(today, True))
        if preset == "last_30_days":
            start = today - timedelta(days=29)
            return (mk_aware(start), mk_aware(today, True))
        if preset == "this_month":
            start = date(today.year, today.month, 1)
            return (mk_aware(start), mk_aware(today, True))
        if preset == "last_month":
            first_this = date(today.year, today.month, 1)
            last_month_end = first_this - timedelta(days=1)
            last_month_start = date(last_month_end.year, last_month_end.month, 1)
            return (mk_aware(last_month_start), mk_aware(last_month_end, True))
        if preset == "this_quarter":
            q_start_month = ((today.month - 1) // 3) * 3 + 1
            start = date(today.year, q_start_month, 1)
            return (mk_aware(start), mk_aware(today, True))
        if preset == "this_year":
            start = date(today.year, 1, 1)
            return (mk_aware(start), mk_aware(today, True))

        return None

    def _is_ymd(self, s: str) -> bool:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s))

    def _serialize(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Convert Decimals/DateTimes to JSON-safe primitives.
        """
        out = []
        for r in rows:
            item = {}
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    item[k] = v.isoformat()
                elif type(v).__name__ == "Decimal":
                    item[k] = float(v)
                else:
                    item[k] = v
            out.append(item)
        return out

    def _stock_balance_annotation(self, date_range: Optional[Tuple[datetime, datetime]] = None):
        """
        Signed sum by movement_type as per inventory rules.
        """
        positive_types = ["receipt", "transfer_in", "adjustment", "production_receipt"]
        negative_types = ["issue", "transfer_out", "production_issue"]

        conditions = []
        if date_range:
            start, end = date_range
            conditions.append(Q(movement_date__gte=start, movement_date__lte=end))

        base_q = Q(tenant=self.tenant)
        for c in conditions:
            base_q &= c

        return Sum(
            Case(
                When(Q(movement_type__in=positive_types) & base_q, then=F("quantity")),
                When(Q(movement_type__in=negative_types) & base_q, then=-F("quantity")),
                default=0,
                output_field=DecimalField(max_digits=18, decimal_places=3)
            )
        )

    # ---------- Domain handlers ----------

    def _handle_products(self, action, filters, limit, order_by):
        qs = Product.objects.filter(tenant=self.tenant, is_active=True)

        # Filters
        if filters.get("product_type"):
            qs = qs.filter(product_type=filters["product_type"])
        if filters.get("category"):
            qs = qs.filter(category=filters["category"])
        if filters.get("text"):
            t = filters["text"]
            qs = qs.filter(Q(sku__icontains=t) | Q(product_name__icontains=t) | Q(category__icontains=t))

        # Ordering
        if order_by:
            qs = qs.order_by(*order_by)
        else:
            qs = qs.order_by("sku")

        rows = list(qs.values("sku", "product_name", "product_type", "uom", "category", "standard_cost", "reorder_point")[:limit])
        data = {"products": self._serialize(rows)}
        summary = f"Found {len(rows)} product(s)"
        return data, summary

    def _handle_inventory(self, action, filters, date_range, group_by, metrics, limit, order_by):
        """
        Supports:
        - current stock per product and/or warehouse
        - optional date filter on movements
        - category/product_type filters
        """
        # Base movements filtered by tenant
        m_qs = StockMovement.objects.filter(tenant=self.tenant)

        # Filter by product attributes via join
        if filters.get("product_type") or filters.get("category") or filters.get("text"):
            m_qs = m_qs.select_related("product")
            if filters.get("product_type"):
                m_qs = m_qs.filter(product__product_type=filters["product_type"])
            if filters.get("category"):
                m_qs = m_qs.filter(product__category=filters["category"])
            if filters.get("text"):
                t = filters["text"]
                m_qs = m_qs.filter(
                    Q(product__sku__icontains=t) |
                    Q(product__product_name__icontains=t) |
                    Q(reference_doc__icontains=t)
                )

        # Filter warehouse
        if filters.get("warehouse_code"):
            m_qs = m_qs.select_related("warehouse").filter(warehouse__warehouse_code=filters["warehouse_code"])

        # Date range constraints applied via annotation builder
        stock_sum = self._stock_balance_annotation(date_range)

        values = ["product__sku", "product__product_name"]
        if "warehouse" in [g.lower() for g in group_by]:
            values.append("warehouse__warehouse_code")

        agg_qs = m_qs.values(*values).annotate(current_stock=stock_sum)

        # Remove rows where annotation is null/zero unless explicitly requested
        agg_qs = agg_qs.filter(~Q(current_stock=None))

        # Ordering
        if order_by:
            agg_qs = agg_qs.order_by(*order_by)
        else:
            agg_qs = agg_qs.order_by("product__sku")

        rows = list(agg_qs[:limit])
        data = {"inventory": self._serialize(rows)}

        # Summary sentence
        scope = []
        if filters.get("category"):
            scope.append(f"category '{filters['category']}'")
        if filters.get("product_type"):
            scope.append(f"type '{filters['product_type']}'")
        if filters.get("warehouse_code"):
            scope.append(f"warehouse '{filters['warehouse_code']}'")
        date_text = self._date_range_text(date_range)
        suffix = f" {date_text}" if date_text else ""
        summary = f"Calculated current stock for {len(rows)} row(s){(' with ' + ', '.join(scope)) if scope else ''}{suffix}."
        return data, summary

    def _handle_work_orders(self, action, filters, date_range, group_by, metrics, limit, order_by):
        qs = WorkOrder.objects.filter(tenant=self.tenant)

        # ✅ Only apply valid filters
        valid_statuses = ["planned", "released", "in_progress", "completed", "cancelled"]
        if filters.get("status") in valid_statuses:
            qs = qs.filter(status=filters["status"])

        if filters.get("product_type"):
            qs = qs.select_related("product").filter(product__product_type=filters["product_type"])

        if filters.get("category"):
            qs = qs.select_related("product").filter(product__category=filters["category"])

        if filters.get("cost_center_code"):
            qs = qs.select_related("cost_center").filter(cost_center__cost_center_code=filters["cost_center_code"])

        if filters.get("text"):
            t = filters["text"]
            qs = qs.filter(
                Q(wo_number__icontains=t) |
                Q(product__sku__icontains=t) |
                Q(product__product_name__icontains=t)
            )

        # ✅ Handle date_range correctly
        if date_range:
            start, end = date_range
            qs = qs.filter(due_date__gte=start.date(), due_date__lte=end.date())

        if order_by:
            qs = qs.order_by(*order_by)
        else:
            qs = qs.order_by("due_date", "wo_number")

        rows = list(qs.values(
            "wo_number", "status", "priority",
            "product__sku", "product__product_name", "product__category",
            "quantity_planned", "quantity_completed", "quantity_scrapped",
            "due_date", "cost_center__cost_center_code"
        )[:limit])

        data = {"work_orders": self._serialize(rows)}
        summary = f"Found {len(rows)} work order(s)"
        return data, summary


    def _handle_production(self, action, filters, date_range, group_by, metrics, limit, order_by):
        qs = ProductionEntry.objects.filter(tenant=self.tenant).select_related("work_order", "equipment", "operator", "work_order__product")

        if filters.get("text"):
            t = filters["text"]
            qs = qs.filter(
                Q(work_order__wo_number__icontains=t) |
                Q(work_order__product__sku__icontains=t) |
                Q(equipment__equipment_code__icontains=t) |
                Q(operator__employee_code__icontains=t)
            )
        if filters.get("product_type"):
            qs = qs.filter(work_order__product__product_type=filters["product_type"])
        if filters.get("category"):
            qs = qs.filter(work_order__product__category=filters["category"])
        if filters.get("cost_center_code"):
            qs = qs.filter(work_order__cost_center__cost_center_code=filters["cost_center_code"])

        if date_range:
            start, end = date_range
            qs = qs.filter(entry_datetime__gte=start, entry_datetime__lte=end)

        values = [
            "work_order__wo_number",
            "work_order__product__sku",
            "equipment__equipment_code",
            "operator__employee_code",
            "entry_datetime",
            "quantity_produced",
            "quantity_rejected",
            "downtime_minutes",
            "shift",
        ]
        if order_by:
            qs = qs.order_by(*order_by)
        else:
            qs = qs.order_by("-entry_datetime")

        rows = list(qs.values(*values)[:limit])
        data = {"production_entries": self._serialize(rows)}

        # Optional KPI summary if requested
        kpi_bits = []
        if not metrics or "output" in metrics or "quantity_produced" in metrics:
            produced = sum([r.get("quantity_produced") or 0 for r in rows])
            kpi_bits.append(f"produced={produced}")
        if not metrics or "rejections" in metrics or "quantity_rejected" in metrics:
            rejected = sum([r.get("quantity_rejected") or 0 for r in rows])
            kpi_bits.append(f"rejected={rejected}")
        if not metrics or "downtime_minutes" in metrics:
            downtime = sum([r.get("downtime_minutes") or 0 for r in rows])
            kpi_bits.append(f"downtime_min={downtime}")

        drt = self._date_range_text(date_range)
        summary = f"Production entries: {', '.join(kpi_bits)}"
        if drt:
            summary += f" {drt}."
        return data, summary

    def _handle_finance(self, action, filters, date_range, group_by, metrics, limit, order_by):
        """
        Lists journals or summarizes by account type/cost center.
        """
        jl = GLJournalLine.objects.filter(tenant=self.tenant).select_related("journal", "account", "cost_center")

        if filters.get("account_type"):
            jl = jl.filter(account__account_type=filters["account_type"])
        if filters.get("cost_center_code"):
            jl = jl.filter(cost_center__cost_center_code=filters["cost_center_code"])

        if date_range:
            start, end = date_range
            jl = jl.filter(journal__posting_date__gte=start.date(), journal__posting_date__lte=end.date())

        values = [
            "journal__journal_number", "journal__posting_date", "journal__status",
            "account__account_code", "account__account_name", "account__account_type",
            "cost_center__cost_center_code",
            "debit_amount", "credit_amount", "description"
        ]

        if order_by:
            jl = jl.order_by(*order_by)
        else:
            jl = jl.order_by("-journal__posting_date", "journal__journal_number", "line_number")

        rows = list(jl.values(*values)[:limit])

        # Optional grouped KPI
        total_debit = sum([r.get("debit_amount") or 0 for r in rows])
        total_credit = sum([r.get("credit_amount") or 0 for r in rows])

        data = {
            "journal_lines": self._serialize(rows),
            "summary": {"total_debit": float(total_debit), "total_credit": float(total_credit)}
        }

        drt = self._date_range_text(date_range)
        scope = []
        if filters.get("account_type"):
            scope.append(f"account_type '{filters['account_type']}'")
        if filters.get("cost_center_code"):
            scope.append(f"cost_center '{filters['cost_center_code']}'")
        suffix = f" {drt}" if drt else ""
        summary = f"Finance lines: {len(rows)} row(s), debit={total_debit}, credit={total_credit}{suffix}."
        if scope:
            summary += f" Filtered by {', '.join(scope)}."
        return data, summary

    def _handle_parties(self, action, filters, limit, order_by):
        qs = Party.objects.filter(tenant=self.tenant, is_active=True)
        if filters.get("party_type"):
            qs = qs.filter(party_type=filters["party_type"])
        if filters.get("text"):
            t = filters["text"]
            qs = qs.filter(
                Q(party_code__icontains=t) |
                Q(legal_name__icontains=t) |
                Q(display_name__icontains=t)
            )
        if order_by:
            qs = qs.order_by(*order_by)
        else:
            qs = qs.order_by("party_code")
        rows = list(qs.values("party_code", "party_type", "display_name", "gstin", "pan", "payment_terms", "credit_limit")[:limit])
        data = {"parties": self._serialize(rows)}
        summary = f"Found {len(rows)} party record(s)"
        return data, summary

    def _handle_equipment(self, action, filters, date_range, group_by, metrics, limit, order_by):
        """
        Equipment listing plus optional KPIs joined from ProductionEntry.
        Supports metric 'downtime_minutes' which is stored on ProductionEntry.
        Returns (data_dict, summary_str)
        """
        # Filter out invalid order_by fields
        valid_order_fields = [
            "equipment_code", "equipment_name", "location", "capacity_per_hour",
            "acquisition_date", "last_maintenance", "next_maintenance", "downtime_minutes"
        ]
        
        # Clean order_by to only include valid fields
        cleaned_order_by = []
        for field in order_by:
            field_name = field.lstrip('-')
            if field_name in valid_order_fields:
                cleaned_order_by.append(field)
        
        # If no valid order_by fields remain, use default
        if not cleaned_order_by:
            cleaned_order_by = ["equipment_code"]
        
        # Base equipment queryset
        eq_qs = Equipment.objects.filter(tenant=self.tenant, is_active=True)

        # simple text filter on equipment
        if filters.get("text"):
            t = filters["text"]
            eq_qs = eq_qs.filter(
                Q(equipment_code__icontains=t) |
                Q(equipment_name__icontains=t) |
                Q(location__icontains=t)
            )

        # Determine if user requested downtime (or ordering by it)
        wants_downtime = False
        if metrics and "downtime_minutes" in metrics:
            wants_downtime = True
        # also if order_by includes -downtime_minutes or downtime_minutes
        for ob in cleaned_order_by:
            if ob.lstrip("-") == "downtime_minutes":
                wants_downtime = True

        # If downtime aggregation requested -> aggregate from ProductionEntry
        if wants_downtime:
            pe_qs = ProductionEntry.objects.filter(tenant=self.tenant).select_related("equipment")
            if date_range:
                start, end = date_range
                pe_qs = pe_qs.filter(entry_datetime__gte=start, entry_datetime__lte=end)

            # Sum downtime per equipment
            from django.db.models import Sum
            agg = pe_qs.values("equipment__id", "equipment__equipment_code", "equipment__equipment_name") \
                        .annotate(total_downtime=Sum("downtime_minutes"))

            # Order by requested order_by if it mentions downtime, else by total_downtime desc
            # Convert Django-style order_by list into workable ordering
            if cleaned_order_by:
                # if user requested ordering by downtime, apply; else default to -total_downtime
                if any(o.lstrip("-") == "downtime_minutes" for o in cleaned_order_by):
                    # translate -downtime_minutes -> '-total_downtime'
                    new_order = []
                    for o in cleaned_order_by:
                        if o.lstrip("-") == "downtime_minutes":
                            if o.startswith("-"):
                                new_order.append("-total_downtime")
                            else:
                                new_order.append("total_downtime")
                        else:
                            # For other fields, we need to check if they're available in the aggregation
                            if o.lstrip("-") in ["equipment_code", "equipment_name"]:
                                # These are available via the equipment__ prefix
                                field_name = o.lstrip("-")
                                prefix = "-" if o.startswith("-") else ""
                                new_order.append(f"{prefix}equipment__{field_name}")
                    agg = agg.order_by(*new_order)
                else:
                    agg = agg.order_by("-total_downtime")
            else:
                agg = agg.order_by("-total_downtime")

            # Limit and build results
            agg = list(agg[:limit])

            equipment_ids = [a.get("equipment__id") for a in agg if a.get("equipment__id")]
            # get equipment core fields for those ids
            eq_rows = {}
            if equipment_ids:
                rows = list(eq_qs.filter(id__in=equipment_ids).values(
                    "id", "equipment_code", "equipment_name", "location", "capacity_per_hour",
                    "acquisition_date", "last_maintenance", "next_maintenance"
                ))
                eq_rows = {r["id"]: r for r in rows}

            result_rows = []
            for a in agg:
                eid = a.get("equipment__id")
                base = eq_rows.get(eid, {
                    "equipment_code": a.get("equipment__equipment_code"),
                    "equipment_name": a.get("equipment__equipment_name"),
                    "location": None,
                    "capacity_per_hour": None,
                    "acquisition_date": None,
                    "last_maintenance": None,
                    "next_maintenance": None
                })
                # attach aggregated downtime
                base_row = {
                    "equipment_code": base.get("equipment_code"),
                    "equipment_name": base.get("equipment_name"),
                    "location": base.get("location"),
                    "capacity_per_hour": base.get("capacity_per_hour"),
                    "acquisition_date": base.get("acquisition_date"),
                    "last_maintenance": base.get("last_maintenance"),
                    "next_maintenance": base.get("next_maintenance"),
                    "total_downtime_minutes": float(a.get("total_downtime") or 0)
                }
                result_rows.append(base_row)

            data = {"equipment": self._serialize(result_rows)}
            summary = f"Found {len(result_rows)} equipment rows annotated with total_downtime_minutes"
            return data, summary

        # Fallback: normal equipment listing (no downtime aggregation)
        if cleaned_order_by:
            eq_qs = eq_qs.order_by(*cleaned_order_by)
        else:
            eq_qs = eq_qs.order_by("equipment_code")

        rows = list(eq_qs.values("equipment_code", "equipment_name", "location", "capacity_per_hour", "acquisition_date", "last_maintenance", "next_maintenance")[:limit])
        data = {"equipment": self._serialize(rows)}
        summary = f"Found {len(rows)} equipment record(s)"
        return data, summary

    def _handle_employees(self, action, filters, limit, order_by):
        qs = Employee.objects.filter(tenant=self.tenant, is_active=True).select_related("cost_center")
        if filters.get("text"):
            t = filters["text"]
            qs = qs.filter(Q(employee_code__icontains=t) | Q(full_name__icontains=t) | Q(department__icontains=t) | Q(designation__icontains=t))
        if filters.get("cost_center_code"):
            qs = qs.filter(cost_center__cost_center_code=filters["cost_center_code"])
        if order_by:
            qs = qs.order_by(*order_by)
        else:
            qs = qs.order_by("employee_code")
        rows = list(qs.values("employee_code", "full_name", "department", "designation", "cost_center__cost_center_code", "hourly_rate", "skill_level", "hire_date")[:limit])
        data = {"employees": self._serialize(rows)}
        summary = f"Found {len(rows)} employee record(s)"
        return data, summary

    # ---------- Utility text formatting ----------

    def _date_range_text(self, date_range: Optional[Tuple[datetime, datetime]]) -> str:
        if not date_range:
            return ""
        start, end = date_range
        return f"from {start.date()} to {end.date()}"

    # At the end of ERPAIEngine

    def _interpret_result(self, domain: str, summary: str, data: Dict[str, Any]) -> Optional[str]:
        """
        Ask LLM for a short opinion. If it fails, provide a fallback based on KPIs.
        """
        try:
            if domain not in ["production", "work_orders", "inventory", "finance"]:
                return None

            produced = rejected = downtime = 0
            if domain == "production":
                rows = data.get("production_entries", []) or []
                produced = sum([r.get("quantity_produced") or 0 for r in rows])
                rejected = sum([r.get("quantity_rejected") or 0 for r in rows])
                downtime = sum([r.get("downtime_minutes") or 0 for r in rows])

            prompt = f"""
    DOMAIN: {domain}
    SUMMARY: {summary}
    KPI: produced={produced}, rejected={rejected}, downtime_min={downtime}

    Task: In 1–2 sentences, say if results are good or bad overall, 
    and suggest one improvement if needed. Keep it professional.
    """
            ai_feedback = call_llm(prompt, response_format="text")

            if isinstance(ai_feedback, str) and ai_feedback.strip():
                return ai_feedback.strip()
            return None
        except Exception:
            return None

