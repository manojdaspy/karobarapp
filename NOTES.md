# Billing Service — Engineering Decisions & Technical Notes

This document accompanies the implementation of **Feature 1 (Returns & Credit Notes)** and **Feature 2 (Smart SMS Payment Reminders)**, along with production security and reliability hardening of the starter repository.

---

## 1. Questions We Would Have Asked & Assumptions Made

| Question for the Business | Assumption Made & Implemented |
|---|---|
| **Return Window & Eligibility**: Is there a time limit on returns (e.g., within 14 or 30 days of invoice date)? Are opened or perishable goods excluded? | We assumed any issued, partially paid, or paid invoice can accept returns regardless of elapsed time, as long as the returned quantity does not exceed the remaining unreturned quantity. |

| **Refund Channels for Paid Invoices**: For invoices already paid via cash, bank transfer, or gateway, should refunds be forced back to the original payment channel? | We assumed the cashier or accountant selects the `refund_method` (`cash`, `bank`, or `store_credit`) on the Credit Note so the shop's physical cash register balances match. |
| **Payment Reminder Scheduling vs Manual Trigger**: Should SMS reminders be 100% automated on a cron schedule, or can shop staff trigger them manually? | We implemented both: an automated Celery task (`send_overdue_payment_reminders`) for daily scheduled runs, and an on-demand API endpoint (`POST /api/invoices/{id}/remind/`). Both paths strictly enforce the same quiet hours and 24-hour rate limits. |
| **Configurable Reminder Policies**: Do different shops have different quiet hours or cooldown needs? | Rather than hardcoding numbers into code, we made cooldown hours (`REMINDER_COOLDOWN_HOURS=24`), maximum attempts (`REMINDER_MAX_ATTEMPTS=3`), and quiet hours (`REMINDER_QUIET_HOURS_START=20`, `REMINDER_QUIET_HOURS_END=8`) configurable via environment variables (`.env`) with sensible defaults for Nepal businesses (`Asia/Kathmandu`). |

---

## 2. Anything Changed in the Existing Code & Why

| File & Area | Change Made | Technical & Business Justification |
|---|---|---|
| **`billing/views.py`** (`OutstandingCustomersView`) | Replaced raw SQL f-string formatting (`LIKE LOWER('%%{search}%%')`) with parameterized Django ORM (`name__icontains=search`, `annotate`, `Coalesce`, `Sum`). | **Security (SQL Injection)**: The original code allowed arbitrary SQL injection and database compromise via the `q` query parameter. The ORM query is 100% parameterized and database-agnostic. |
| **`billing/views.py`** (`InvoiceViewSet.payments`) | Changed `Invoice.objects.get(pk=pk)` to `self.get_object()`. | **Security (Cross-Tenant IDOR)**: The original code fetched invoices globally across all businesses, allowing cashiers from Shop A to post payments against Shop B's invoices. `self.get_object()` enforces tenant isolation. |
| **`billing/services.py`** (`calculate_totals`, `create_invoice`) | Converted all currency math from Python `float` to `Decimal` with `ROUND_HALF_UP`. | **Financial Accuracy**: Binary floating-point arithmetic introduces IEEE 754 precision drift on currency and 13% VAT calculations. |
| **`billing/services.py`** (`create_invoice`) | Added `select_for_update()` row locking on `Product` models in consistent PK order inside an atomic transaction. | **Concurrency (Inventory Overselling)**: The original code read stock without locking, causing negative stock under concurrent checkouts. |
| **`billing/services.py`** (`create_invoice`) | Wrapped `send_invoice_email.delay()` in `transaction.on_commit()`. | **Celery Reliability**: Fired before the database transaction committed, causing workers to fail with `DoesNotExist` or send emails for rolled-back orders. |
| **`billing/tasks.py`** (`process_gateway_payment`) | Added `UniqueConstraint` on `Payment.gateway_ref` and wrapped in `try/except IntegrityError`. Moved SMS dispatch outside transaction. | **Idempotency & Double Charge Prevention**: Webhook retries and Celery failures previously caused duplicate payment records and duplicate SMS receipts. |
| **`billing/views.py`** (`SalesReportView`) | Updated date filtering from `created_at__range=(start, end)` to `created_at__date__gte=start, created_at__date__lte=end`, and deducted `CreditNote` totals and VAT reversals. | **Accounting & Reporting Accuracy**: Truncating the `to` date at midnight excluded all sales on the final day. Sales reports now also accurately reflect net sales after returns. |
| **`billing/serializers.py`** (`PaymentCreateSerializer`) | Added `min_value=Decimal("0.01")` and validated payment amount against `invoice.balance`. | **Data Integrity**: Prevented negative payments, zero payments, and overpayments. |
| **`billing/models.py`** (`Invoice`, `CreditNote`, `Payment`) | Added database `UniqueConstraints` for `(business, number)` and `gateway_ref`. | **Legal & Concurrency Integrity**: Prevented duplicate invoice or credit note numbers under concurrent requests. |

---

## 3. What We Would Build Next With Another Day

1. **IRD-Compliant PDF Credit Notes & Invoices**:
   - Integrate WeasyPrint or ReportLab to render printable, bilingual (English/Nepali) PDF tax invoices and credit notes conforming to Nepal Inland Revenue Department (IRD) requirements, including shop PAN, customer PAN, QR verification codes, and cashier signatures.
2. **Customer Wallet / Store Credit Ledger**:
   - When a return is settled via `store_credit`, credit an internal customer wallet balance that can be automatically redeemed as a payment method (`Payment.Method.STORE_CREDIT`) on subsequent invoices.
3. **Multi-Channel Reminders & Customer Opt-Out (DND)**:
   - Add WhatsApp Business API notifications as a high-engagement channel; implement a regulatory opt-out mechanism (e.g. customer replies "STOP" to prevent further automated messages).
4. **Shop-Level Notification Preferences UI**:
   - Provide shop owners with a settings interface to configure their own custom quiet hours, reminder frequencies, and personalized SMS copy per business rather than global environment variables.
5. **High-Concurrency Stress Testing on PostgreSQL**:
   - Build automated load tests using Locust or `concurrent.futures` to simulate 100 concurrent checkouts and webhook spikes against PostgreSQL to measure lock contention and latency under load.
