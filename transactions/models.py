from django.db import models


class Account(models.Model):
    class AccountType(models.TextChoices):
        CREDIT_CARD = 'CREDIT_CARD', 'Credit card'
        CHECKING = 'CHECKING', 'Checking'
        SAVINGS = 'SAVINGS', 'Savings'

    institution = models.CharField(max_length=64, help_text="The financial institution that issued this account, e.g. 'Chase'.")
    account_identifier = models.CharField(max_length=128, help_text="Identifier used by the institution, e.g. ACCTID from a QFX file.")
    account_type = models.CharField(max_length=16, choices=AccountType.choices)
    default_currency = models.CharField(max_length=3, help_text="The account's own currency; the currency transactions settle in.")
    nickname = models.CharField(max_length=128, blank=True, help_text="Optional user-facing label.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['institution', 'account_identifier'],
                name='uniq_account_institution_identifier',
            ),
        ]

    def __str__(self):
        return self.nickname or f"{self.institution} {self.account_identifier}"


class StatementImport(models.Model):
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name='imports')
    source_file = models.CharField(max_length=1024)
    parser_name = models.CharField(max_length=64)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    ledger_balance_minor = models.BigIntegerField(null=True, blank=True, help_text="Statement-reported ledger balance, in minor units of the account's default currency.")
    available_balance_minor = models.BigIntegerField(null=True, blank=True, help_text="Statement-reported available balance, in minor units of the account's default currency.")
    transaction_count = models.IntegerField(null=True, blank=True, help_text="Number of transactions parsed from the statement.")
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-imported_at']

    def __str__(self):
        return f"{self.parser_name} import on {self.imported_at:%Y-%m-%d} for {self.account}"


class Transaction(models.Model):
    class TransactionType(models.TextChoices):
        DEBIT = 'DEBIT', 'Debit'
        CREDIT = 'CREDIT', 'Credit'

    class Status(models.TextChoices):
        PARSED = 'PARSED', 'Parsed'
        ENRICHED = 'ENRICHED', 'Enriched'
        NEEDS_REVIEW = 'NEEDS_REVIEW', 'Needs review'

    # Ingestion metadata.
    account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='transactions',
        help_text="The account this transaction belongs to. Nullable for legacy rows created before accounts were modeled.",
    )
    source_transaction_id = models.CharField(
        max_length=128,
        null=True,
        blank=True,
        db_index=True,
        help_text="Bank-assigned unique transaction ID (e.g. QFX FITID). Null for formats that do not provide one.",
    )
    statement_import = models.ForeignKey(
        StatementImport,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='transactions',
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PARSED,
    )

    posted_date = models.DateField()
    transaction_date = models.DateField(null=True, blank=True, help_text="The date the transaction was actually made.")
    description_raw = models.TextField(help_text="The raw transaction description from the statement.")

    # The amount that was ultimately settled in the account's currency.
    transaction_type = models.CharField(max_length=6, choices=TransactionType.choices)
    settlement_amount_minor = models.BigIntegerField()
    settlement_currency = models.CharField(max_length=3)

    # The original transaction amount, if different from the settlement currency (e.g., for foreign transactions).
    transaction_amount_minor = models.BigIntegerField(null=True, blank=True)
    transaction_currency = models.CharField(max_length=3, null=True, blank=True)

    # The exchange rate applied, if it was a foreign transaction.
    exchange_rate = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)

    # Legacy dedup key used by formats without a bank-assigned ID (e.g. the old PDF parser).
    # Real uniqueness for QFX-sourced rows is enforced by the partial unique constraint on
    # (account, source_transaction_id) below.
    hash_dedupe = models.CharField(max_length=128, blank=True)
    source_file = models.CharField(max_length=1024, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'source_transaction_id'],
                condition=models.Q(source_transaction_id__isnull=False),
                name='uniq_transaction_account_source_id',
            ),
        ]
