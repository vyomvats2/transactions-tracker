"""Schema-level tests for the new Account / StatementImport models and
the Transaction partial unique constraint.
"""
from __future__ import annotations

import pytest
from django.db import IntegrityError, transaction as db_transaction

from transactions.models import Account, StatementImport, Transaction


@pytest.fixture
def account(db):
    return Account.objects.create(
        institution="Chase",
        account_identifier="TEST-1",
        account_type=Account.AccountType.CREDIT_CARD,
        default_currency="USD",
    )


@pytest.fixture
def other_account(db):
    return Account.objects.create(
        institution="Chase",
        account_identifier="TEST-2",
        account_type=Account.AccountType.CREDIT_CARD,
        default_currency="USD",
    )


def _make_txn(**kwargs):
    defaults = dict(
        posted_date="2025-01-01",
        description_raw="synthetic",
        transaction_type=Transaction.TransactionType.DEBIT,
        settlement_amount_minor=100,
        settlement_currency="USD",
    )
    defaults.update(kwargs)
    return Transaction.objects.create(**defaults)


class TestAccountUniqueness:
    def test_same_institution_and_identifier_collides(self, db):
        Account.objects.create(
            institution="Chase",
            account_identifier="DUP-1",
            account_type=Account.AccountType.CREDIT_CARD,
            default_currency="USD",
        )
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                Account.objects.create(
                    institution="Chase",
                    account_identifier="DUP-1",
                    account_type=Account.AccountType.CREDIT_CARD,
                    default_currency="USD",
                )

    def test_same_identifier_different_institution_is_allowed(self, db):
        Account.objects.create(
            institution="Chase",
            account_identifier="SHARED-ID",
            account_type=Account.AccountType.CREDIT_CARD,
            default_currency="USD",
        )
        # Different institution — should succeed.
        Account.objects.create(
            institution="HSBC",
            account_identifier="SHARED-ID",
            account_type=Account.AccountType.CHECKING,
            default_currency="GBP",
        )
        assert Account.objects.count() == 2


class TestTransactionPartialUnique:
    def test_same_account_and_source_id_collides(self, account):
        _make_txn(account=account, source_transaction_id="FITID-1")
        with pytest.raises(IntegrityError):
            with db_transaction.atomic():
                _make_txn(account=account, source_transaction_id="FITID-1")

    def test_same_source_id_different_account_is_allowed(self, account, other_account):
        _make_txn(account=account, source_transaction_id="FITID-X")
        _make_txn(account=other_account, source_transaction_id="FITID-X")
        assert Transaction.objects.filter(source_transaction_id="FITID-X").count() == 2

    def test_multiple_null_source_ids_for_same_account_are_allowed(self, account):
        # The partial unique index only applies where source_transaction_id IS NOT NULL.
        _make_txn(account=account, source_transaction_id=None, description_raw="a")
        _make_txn(account=account, source_transaction_id=None, description_raw="b")
        _make_txn(account=account, source_transaction_id=None, description_raw="c")
        assert Transaction.objects.filter(
            account=account, source_transaction_id__isnull=True
        ).count() == 3


class TestStatementImportSetNull:
    def test_deleting_statement_import_nulls_out_transaction_fk(self, account):
        imp = StatementImport.objects.create(
            account=account,
            source_file="/tmp/fake.qfx",
            parser_name="chase_credit_qfx",
        )
        txn = _make_txn(
            account=account,
            source_transaction_id="FITID-SI",
            statement_import=imp,
        )
        assert txn.statement_import_id == imp.id

        imp.delete()
        txn.refresh_from_db()
        assert txn.statement_import is None
        # Transaction itself is preserved.
        assert Transaction.objects.filter(pk=txn.pk).exists()
