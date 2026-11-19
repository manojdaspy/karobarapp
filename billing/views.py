import hashlib
import hmac
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import (
    InsufficientStock,
    InvalidPaymentAmount,
    InvoiceCancellationNotAllowed,
    ReturnNotAllowed,
    ReturnQuantityExceeded,
)
from .models import CreditNote, Customer, Invoice, Product
from .serializers import (
    CreateReturnSerializer,
    CreditNoteSerializer,
    CustomerSerializer,
    InvoiceCreateSerializer,
    InvoiceSerializer,
    PaymentCreateSerializer,
    ProductSerializer,
)
from .services import (
    cancel_invoice,
    create_invoice,
    process_return,
    record_payment,
    send_invoice_reminder,
)
from .tasks import process_gateway_payment


def current_business(request):
    return request.user.profile.business


class CustomerViewSet(viewsets.ModelViewSet):
    serializer_class = CustomerSerializer

    def get_queryset(self):
        return Customer.objects.filter(business=current_business(self.request)).order_by("name")

    def perform_create(self, serializer):
        serializer.save(business=current_business(self.request))


class ProductViewSet(viewsets.ModelViewSet):
    serializer_class = ProductSerializer

    def get_queryset(self):
        return Product.objects.filter(business=current_business(self.request)).order_by("sku")

    def perform_create(self, serializer):
        serializer.save(business=current_business(self.request))


class InvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = InvoiceSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = Invoice.objects.filter(business=current_business(self.request))
        status_param = self.request.query_params.get("status")
        if status_param:
            queryset = queryset.filter(status=status_param)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = InvoiceCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        business = current_business(request)

        try:
            invoice = create_invoice(
                business=business,
                customer=Customer.objects.get(pk=data["customer_id"], business=business),
                due_date=data["due_date"],
                items=data["items"],
                user=request.user,
            )
        except InsufficientStock as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Product.DoesNotExist:
            return Response({"detail": "Product not found."}, status=status.HTTP_400_BAD_REQUEST)

        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def payments(self, request, pk=None):
        invoice = self.get_object()
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            payment = record_payment(
                invoice=invoice,
                amount=serializer.validated_data["amount"],
                method=serializer.validated_data["method"],
                user=request.user,
                gateway_ref=serializer.validated_data.get("reference", ""),
            )
        except InvalidPaymentAmount as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"id": payment.id, "status": invoice.status, "balance": str(invoice.balance)},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        invoice = self.get_object()
        try:
            cancel_invoice(invoice)
        except InvoiceCancellationNotAllowed as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=["post"])
    def returns(self, request, pk=None):
        invoice = self.get_object()
        serializer = CreateReturnSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            credit_note = process_return(
                invoice=invoice,
                items=serializer.validated_data["items"],
                reason=serializer.validated_data.get("reason", ""),
                refund_method=serializer.validated_data.get(
                    "refund_method", CreditNote.RefundMethod.INVOICE_OFFSET
                ),
                user=request.user,
            )
        except (ReturnNotAllowed, ReturnQuantityExceeded) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(CreditNoteSerializer(credit_note).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def remind(self, request, pk=None):
        invoice = self.get_object()
        success, message = send_invoice_reminder(invoice)
        status_code = status.HTTP_200_OK if success else status.HTTP_400_BAD_REQUEST
        return Response({"success": success, "detail": message}, status=status_code)


class CreditNoteViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CreditNoteSerializer

    def get_queryset(self):
        return CreditNote.objects.filter(business=current_business(self.request)).order_by(
            "-created_at"
        )


class SalesReportView(APIView):
    def get(self, request):
        business = current_business(request)
        today = timezone.localdate()
        start = parse_date(request.query_params.get("from", "")) or today.replace(day=1)
        end = parse_date(request.query_params.get("to", "")) or today

        invoices = Invoice.objects.filter(
            business=business, created_at__date__gte=start, created_at__date__lte=end
        ).exclude(status=Invoice.Status.CANCELLED)

        totals = invoices.aggregate(
            count=Count("id"),
            gross=Sum("total"),
            vat=Sum("tax"),
            collected=Sum("amount_paid"),
        )
        overdue = invoices.filter(
            due_date__lt=today,
            status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID],
        ).count()

        # Deduct returns / credit notes issued in this period
        credit_notes = CreditNote.objects.filter(
            business=business, created_at__date__gte=start, created_at__date__lte=end
        )
        credit_totals = credit_notes.aggregate(
            returns_gross=Sum("total"),
            returns_vat=Sum("tax"),
        )
        returns_gross = credit_totals["returns_gross"] or Decimal("0.00")
        returns_vat = credit_totals["returns_vat"] or Decimal("0.00")

        gross_sales = (totals["gross"] or Decimal("0.00")) - returns_gross
        vat_collected = (totals["vat"] or Decimal("0.00")) - returns_vat

        return Response(
            {
                "from": start,
                "to": end,
                "invoice_count": totals["count"],
                "gross_sales": gross_sales,
                "vat_collected": vat_collected,
                "amount_collected": totals["collected"] or Decimal("0.00"),
                "overdue_invoices": overdue,
                "returns_total": returns_gross,
            }
        )


class OutstandingCustomersView(APIView):
    def get(self, request):
        business = current_business(request)
        search = request.query_params.get("q", "").strip()

        customers = Customer.objects.filter(business=business)
        if search:
            customers = customers.filter(name__icontains=search)

        customers = (
            customers.annotate(
                outstanding=Coalesce(
                    Sum(
                        F("invoices__total")
                        - F("invoices__amount_paid")
                        - F("invoices__amount_credited"),
                        filter=Q(
                            invoices__status__in=[
                                Invoice.Status.ISSUED,
                                Invoice.Status.PARTIALLY_PAID,
                            ]
                        ),
                    ),
                    Decimal("0.00"),
                )
            )
            .order_by("-outstanding", "name")[:50]
        )

        rows = [
            {
                "id": c.id,
                "name": c.name,
                "phone": c.phone,
                "outstanding": c.outstanding,
            }
            for c in customers
        ]
        return Response(rows)


class GatewayWebhookView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        signature = request.headers.get("X-Paygate-Signature", "")
        expected = hmac.new(
            settings.PAYMENT_GATEWAY["WEBHOOK_SECRET"].encode(),
            request.body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        if request.data.get("event") == "payment.succeeded":
            process_gateway_payment.delay(request.data["data"])

        return Response({"received": True})
