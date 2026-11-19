import hashlib
import hmac

from django.conf import settings
from django.db import connection
from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import InsufficientStock
from .models import Customer, Invoice, Product
from .serializers import (
    CustomerSerializer,
    InvoiceCreateSerializer,
    InvoiceSerializer,
    PaymentCreateSerializer,
    ProductSerializer,
)
from .services import cancel_invoice, create_invoice, record_payment
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
        invoice = Invoice.objects.get(pk=pk)
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payment = record_payment(
            invoice=invoice,
            amount=serializer.validated_data["amount"],
            method=serializer.validated_data["method"],
            user=request.user,
            gateway_ref=serializer.validated_data.get("reference", ""),
        )
        return Response(
            {"id": payment.id, "status": invoice.status, "balance": str(invoice.balance)},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        invoice = self.get_object()
        if invoice.status in (Invoice.Status.PAID, Invoice.Status.PARTIALLY_PAID):
            return Response(
                {"detail": "Invoices with payments cannot be cancelled."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if invoice.status == Invoice.Status.CANCELLED:
            return Response({"detail": "Already cancelled."}, status=status.HTTP_400_BAD_REQUEST)

        cancel_invoice(invoice)
        return Response(InvoiceSerializer(invoice).data)


class SalesReportView(APIView):
    def get(self, request):
        business = current_business(request)
        today = timezone.localdate()
        start = parse_date(request.query_params.get("from", "")) or today.replace(day=1)
        end = parse_date(request.query_params.get("to", "")) or today

        invoices = Invoice.objects.filter(
            business=business, created_at__range=(start, end)
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

        return Response(
            {
                "from": start,
                "to": end,
                "invoice_count": totals["count"],
                "gross_sales": totals["gross"] or 0,
                "vat_collected": totals["vat"] or 0,
                "amount_collected": totals["collected"] or 0,
                "overdue_invoices": overdue,
            }
        )


class OutstandingCustomersView(APIView):
    def get(self, request):
        business = current_business(request)
        search = request.query_params.get("q", "")

        sql = f"""
            SELECT c.id, c.name, c.phone,
                   COALESCE(SUM(i.total - i.amount_paid), 0) AS outstanding
            FROM billing_customer c
            LEFT JOIN billing_invoice i
                   ON i.customer_id = c.id
                  AND i.status IN ('issued', 'partially_paid')
            WHERE c.business_id = %s
              AND LOWER(c.name) LIKE LOWER('%%{search}%%')
            GROUP BY c.id, c.name, c.phone
            ORDER BY outstanding DESC
            LIMIT 50
        """
        with connection.cursor() as cursor:
            cursor.execute(sql, [business.id])
            columns = [col[0] for col in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

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
