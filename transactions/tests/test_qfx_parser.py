"""Tests for ChaseCreditQFXParser against the anonymized fixture."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from transactions.models import Account, StatementImport, Transaction
from transactions.parsers import ChaseCreditQFXParser

FIXTURES = Path(__file__).parent / "fixtures"
CHASE_SAMPLE = FIXTURES / "chase_credit_sample.qfx"
NON_CHASE = FIXTURES / "non_chase.qfx"
BANK_ONLY = FIXTURES / "bank_only.qfx"

# These values are sensitive to the anonymization factors. Keep them in
# sync with anonymize.py (AMOUNT_JITTER=1.13, DATE_OFFSET=+365 days).
EXPECTED_TXN_COUNT = 132
EXPECTED_PERIOD_START = "2025-12-19"
EXPECTED_PERIOD_END = "2026-01-18"


def run_parser(path: Path) -> None:
    parser = ChaseCreditQFXParser(statement_path=str(path))
    parser.parse()


class TestHappyPath:
    def test_parses_fixture_into_expected_row_count(self, db):
        run_parser(CHASE_SAMPLE)
        assert Transaction.objects.count() == EXPECTED_TXN_COUNT

    def test_auto_creates_account(self, db):
        run_parser(CHASE_SAMPLE)
        account = Account.objects.get(
            institution="Chase", account_identifier="000000000-TEST"
        )
        assert account.account_type == Account.AccountType.CREDIT_CARD
        assert account.default_currency == "USD"

    def test_creates_statement_import_with_metadata(self, db):
        run_parser(CHASE_SAMPLE)
        imp = StatementImport.objects.get(parser_name="chase_credit_qfx")
        assert str(imp.period_start) == EXPECTED_PERIOD_START
        assert str(imp.period_end) == EXPECTED_PERIOD_END
        assert imp.transaction_count == EXPECTED_TXN_COUNT
        assert imp.ledger_balance_minor is not None
        assert imp.available_balance_minor is not None

    def test_all_rows_linked_to_account_and_import(self, db):
        run_parser(CHASE_SAMPLE)
        account = Account.objects.get(account_identifier="000000000-TEST")
        imp = StatementImport.objects.get(parser_name="chase_credit_qfx")
        linked = Transaction.objects.filter(account=account, statement_import=imp)
        assert linked.count() == EXPECTED_TXN_COUNT


class TestIdempotency:
    def test_rerun_produces_no_new_rows(self, db):
        run_parser(CHASE_SAMPLE)
        first_ids = set(Transaction.objects.values_list("id", flat=True))
        first_created_ats = dict(
            Transaction.objects.values_list("id", "created_at")
        )

        run_parser(CHASE_SAMPLE)
        second_ids = set(Transaction.objects.values_list("id", flat=True))
        second_created_ats = dict(
            Transaction.objects.values_list("id", "created_at")
        )

        assert first_ids == second_ids
        # created_at must NOT change on re-import — we're using get_or_create.
        assert first_created_ats == second_created_ats


class TestSignMapping:
    """TRNTYPE drives transaction_type; sign of TRNAMT does not."""

    def test_debit_negative_trnamt_maps_correctly(self, db):
        run_parser(CHASE_SAMPLE)
        # Every DEBIT row in the sample has a negative TRNAMT; settlement
        # must be positive, type must be DEBIT.
        debits = Transaction.objects.filter(
            transaction_type=Transaction.TransactionType.DEBIT
        )
        assert debits.count() > 0
        assert all(t.settlement_amount_minor > 0 for t in debits)

    def test_credit_positive_trnamt_maps_correctly(self, db):
        run_parser(CHASE_SAMPLE)
        credits = Transaction.objects.filter(
            transaction_type=Transaction.TransactionType.CREDIT
        )
        assert credits.count() > 0  # the anonymized file keeps the payment CREDIT row.
        assert all(t.settlement_amount_minor > 0 for t in credits)


class TestRejectionFixtures:
    def test_non_chase_raises_and_writes_nothing(self, db):
        with pytest.raises(ValueError, match="Chase QFX"):
            run_parser(NON_CHASE)
        assert Transaction.objects.count() == 0
        assert Account.objects.count() == 0
        assert StatementImport.objects.count() == 0

    def test_bank_only_raises_and_writes_nothing(self, db):
        with pytest.raises(ValueError, match="credit-card"):
            run_parser(BANK_ONLY)
        assert Transaction.objects.count() == 0
        assert Account.objects.count() == 0
        assert StatementImport.objects.count() == 0


class TestEnrichmentPreservation:
    def test_qfx_reimport_preserves_fx_fields_and_enriched_status(self, db):
        run_parser(CHASE_SAMPLE)
        # Simulate PDF enrichment on one row: attach FX data and flip to ENRICHED.
        txn = Transaction.objects.filter(
            transaction_type=Transaction.TransactionType.DEBIT
        ).first()
        txn.transaction_amount_minor = 280
        txn.transaction_currency = "GBP"
        txn.exchange_rate = Decimal("1.23456789")
        txn.status = Transaction.Status.ENRICHED
        txn.save()

        # Re-run QFX. The ENRICHED row's FX data and status must survive.
        run_parser(CHASE_SAMPLE)

        txn.refresh_from_db()
        assert txn.status == Transaction.Status.ENRICHED
        assert txn.transaction_amount_minor == 280
        assert txn.transaction_currency == "GBP"
        assert txn.exchange_rate == Decimal("1.23456789")

    def test_qfx_reimport_resets_non_enriched_status_to_parsed(self, db):
        run_parser(CHASE_SAMPLE)
        # Flag a row as NEEDS_REVIEW (simulating an ambiguous PDF match).
        txn = Transaction.objects.first()
        txn.status = Transaction.Status.NEEDS_REVIEW
        txn.save()

        # Re-run QFX. Non-ENRICHED status should reset to PARSED.
        run_parser(CHASE_SAMPLE)
        txn.refresh_from_db()
        assert txn.status == Transaction.Status.PARSED
