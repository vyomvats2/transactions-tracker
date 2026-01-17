from django.contrib import admin
from .models import Transaction

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('posted_date', 'description_raw', 'get_amount', 'transaction_type', 'source_file')
    search_fields = ('description_raw', 'hash_dedupe')
    list_filter = ('transaction_type', 'settlement_currency', 'posted_date')
    ordering = ('-posted_date',)

    def get_amount(self, obj):
        return f"{obj.settlement_amount_minor / 100:.2f} {obj.settlement_currency}"
    get_amount.short_description = 'Settlement Amount'
