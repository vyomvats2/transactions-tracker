from django.contrib import admin
from .models import Account, StatementImport, Transaction


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('institution', 'account_identifier', 'account_type', 'default_currency', 'nickname')
    list_filter = ('institution', 'account_type', 'default_currency')
    search_fields = ('institution', 'account_identifier', 'nickname')
    list_editable = ('nickname',)


@admin.register(StatementImport)
class StatementImportAdmin(admin.ModelAdmin):
    list_display = ('imported_at', 'account', 'parser_name', 'period_start', 'period_end', 'transaction_count')
    list_filter = ('parser_name', 'account')
    date_hierarchy = 'imported_at'
    readonly_fields = ('imported_at',)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ('posted_date', 'description_raw', 'get_amount', 'transaction_type', 'status', 'account', 'source_file')
    list_filter = ('status', 'account', 'transaction_type', 'settlement_currency', 'posted_date')
    search_fields = ('description_raw', 'hash_dedupe', 'source_transaction_id')
    ordering = ('-posted_date',)

    def get_amount(self, obj):
        return f"{obj.settlement_amount_minor / 100:.2f} {obj.settlement_currency}"
    get_amount.short_description = 'Settlement Amount'
