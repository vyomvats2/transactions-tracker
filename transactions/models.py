from django.db import models

class Transaction(models.Model):
    class TransactionType(models.TextChoices):
        DEBIT = 'DEBIT', 'Debit'
        CREDIT = 'CREDIT', 'Credit'

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

    hash_dedupe = models.CharField(max_length=64, unique=True)
    source_file = models.CharField(max_length=1024, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
