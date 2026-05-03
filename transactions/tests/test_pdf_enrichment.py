"""Tests for ChaseCreditPDFParser's enrichment logic.

These tests seed QFX-style Transaction rows directly via the ORM and then
invoke the PDF parser's private enrichment helpers against synthetic PDF
tuples. This isolates the matching/enrichment logic from the pdfplumber
text-extraction walker, which is already proven against real statements.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from transactions.models import Account, StatementImport, Transaction
from transactions.parsers import ChaseCreditPDFParser


@pytest.fixture
def account(db):
    return Account.objects.create(
        institution="Chase",
        account_identifier="TEST-4412",
        account_type=Account.AccountType.CREDIT_CARD,
        default_currency="USD",
    )


@pytest.fixture
def import_record(account):
    return StatementImport.objects.create(
        account=account,
        source_file="/tmp/fake.pdf",
        parser_name="chase_credit_pdf",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
    )


@pytest.fixture
def parser():
    return ChaseCreditPDFParser(statement_path="/tmp/fake.pdf")


def _qfx_txn(account, **kwargs):
    defaults = dict(
        description_raw="SOMETHING",
        transaction_type=Transaction.TransactionType.DEBIT,
        settlement_amount_minor=1000,
        settlement_currency="USD",
        status=Transaction.Status.PARSED,
    )
    defaults.update(kwargs)
    defaults.setdefault("source_transaction_id", f"FITID-{Transaction.objects.count() + 1}")
    return Transaction.objects.create(account=account, **defaults)


def _pdf_tuple(**overrides):
    tup = {
        "posted_date": date(2025, 1, 10),
        "transaction_date": date(2025, 1, 10),
        "description_raw": "SOMETHING",
        "transaction_type": Transaction.TransactionType.DEBIT,
        "settlement_amount_minor": 1000,
        "settlement_currency": "USD",
        "transaction_amount_minor": None,
        "transaction_currency": None,
        "exchange_rate": None,
    }
    tup.update(overrides)
    return tup


class TestSingleCandidate:
    def test_exact_match_with_fx_enriches(self, account, import_record, parser):
        qfx = _qfx_txn(
            account,
            posted_date=date(2025, 1, 10),
            description_raw="WAITROSE 664",
            settlement_amount_minor=2177,
        )
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),
            description_raw="WAITROSE 664 LONDON",
            settlement_amount_minor=2177,
            transaction_amount_minor=1700,
            transaction_currency="GBP",
            exchange_rate=Decimal("1.28058823"),
        )

        outcome = parser._enrich_one(tup, account, import_record)

        assert outcome == "enriched"
        qfx.refresh_from_db()
        assert qfx.status == Transaction.Status.ENRICHED
        assert qfx.transaction_amount_minor == 1700
        assert qfx.transaction_currency == "GBP"
        assert qfx.exchange_rate == Decimal("1.28058823")
        assert qfx.statement_import_id == import_record.id

    def test_same_currency_match_flips_status_without_fx(self, account, import_record, parser):
        qfx = _qfx_txn(
            account,
            posted_date=date(2025, 1, 15),
            description_raw="AUTOMATIC PAYMENT - THANK",
            settlement_amount_minor=829716,
            transaction_type=Transaction.TransactionType.CREDIT,
        )
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 15),
            description_raw="AUTOMATIC PAYMENT - THANK",
            transaction_type=Transaction.TransactionType.CREDIT,
            settlement_amount_minor=829716,
            # No FX data (domestic row)
        )

        outcome = parser._enrich_one(tup, account, import_record)

        assert outcome == "enriched"
        qfx.refresh_from_db()
        assert qfx.status == Transaction.Status.ENRICHED
        assert qfx.transaction_amount_minor is None
        assert qfx.transaction_currency is None
        assert qfx.exchange_rate is None
        assert qfx.statement_import_id == import_record.id


class TestDateWindow:
    """PDF posted_date can be 1-3 days earlier than QFX DTPOSTED."""

    def test_window_matches_two_days_later(self, account, import_record, parser):
        qfx = _qfx_txn(
            account,
            posted_date=date(2025, 1, 12),  # QFX says posted on Jan 12
            description_raw="GOOGLE *ChatGPT",
            settlement_amount_minor=1999,
        )
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),  # PDF says 10 (2 days earlier)
            description_raw="GOOGLE *ChatGPT 855-836-3987 CA",
            settlement_amount_minor=1999,
        )
        outcome = parser._enrich_one(tup, account, import_record)
        assert outcome == "enriched"
        qfx.refresh_from_db()
        assert qfx.status == Transaction.Status.ENRICHED

    def test_window_does_not_match_beyond_lag(self, account, import_record, parser):
        # Five days after PDF_date — outside window of 4.
        _qfx_txn(
            account,
            posted_date=date(2025, 1, 15),
            description_raw="SOMETHING",
            settlement_amount_minor=500,
        )
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),
            settlement_amount_minor=500,
        )
        outcome = parser._enrich_one(tup, account, import_record)
        assert outcome == "synthetic"


class TestDisambiguationBySubstring:
    def test_multiple_candidates_narrowed_to_one_by_description(
        self, account, import_record, parser
    ):
        # Two candidates match the base filter; only one matches description.
        qfx_match = _qfx_txn(
            account,
            posted_date=date(2025, 1, 10),
            description_raw="WAITROSE 664",
            settlement_amount_minor=1000,
        )
        qfx_other = _qfx_txn(
            account,
            posted_date=date(2025, 1, 10),
            description_raw="TESCO STORES",
            settlement_amount_minor=1000,
        )
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),
            description_raw="WAITROSE 664 CANARY WHARF",
            settlement_amount_minor=1000,
            transaction_amount_minor=800,
            transaction_currency="GBP",
            exchange_rate=Decimal("1.25"),
        )

        outcome = parser._enrich_one(tup, account, import_record)

        assert outcome == "enriched"
        qfx_match.refresh_from_db()
        qfx_other.refresh_from_db()
        assert qfx_match.status == Transaction.Status.ENRICHED
        assert qfx_match.transaction_amount_minor == 800
        assert qfx_other.status == Transaction.Status.PARSED  # untouched

    def test_different_descriptions_neither_matches_flags_all(
        self, account, import_record, parser
    ):
        a = _qfx_txn(
            account,
            posted_date=date(2025, 1, 10),
            description_raw="MERCHANT A",
            settlement_amount_minor=500,
        )
        b = _qfx_txn(
            account,
            posted_date=date(2025, 1, 10),
            description_raw="MERCHANT B",
            settlement_amount_minor=500,
        )
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),
            description_raw="UNRELATED THIRD PARTY",
            settlement_amount_minor=500,
        )

        outcome = parser._enrich_one(tup, account, import_record)

        assert outcome == "ambiguous"
        a.refresh_from_db()
        b.refresh_from_db()
        assert a.status == Transaction.Status.NEEDS_REVIEW
        assert b.status == Transaction.Status.NEEDS_REVIEW


class TestUniformCluster:
    """Same description, multiple candidates — fill un-enriched one at a time."""

    def test_first_tuple_enriches_first_unenriched_candidate(
        self, account, import_record, parser
    ):
        # Four identical TFL $3.52 rows on adjacent days.
        qfx_rows = [
            _qfx_txn(
                account,
                posted_date=date(2025, 1, 1),
                description_raw="TFL TRAVEL CH",
                settlement_amount_minor=352,
            ),
            _qfx_txn(
                account,
                posted_date=date(2025, 1, 1),
                description_raw="TFL TRAVEL CH",
                settlement_amount_minor=352,
            ),
            _qfx_txn(
                account,
                posted_date=date(2025, 1, 2),
                description_raw="TFL TRAVEL CH",
                settlement_amount_minor=352,
            ),
            _qfx_txn(
                account,
                posted_date=date(2025, 1, 2),
                description_raw="TFL TRAVEL CH",
                settlement_amount_minor=352,
            ),
        ]
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 1),
            description_raw="TFL TRAVEL CH TFL.GOV.UK/CP",
            settlement_amount_minor=352,
            transaction_amount_minor=280,
            transaction_currency="GBP",
            exchange_rate=Decimal("1.25714"),
        )

        outcome = parser._enrich_one(tup, account, import_record)
        assert outcome == "enriched"

        # The earliest (by posted_date, id) un-enriched candidate should be picked.
        for r in qfx_rows:
            r.refresh_from_db()
        enriched = [r for r in qfx_rows if r.status == Transaction.Status.ENRICHED]
        assert len(enriched) == 1
        assert enriched[0].posted_date == date(2025, 1, 1)
        assert enriched[0].transaction_amount_minor == 280

    def test_cluster_filled_across_multiple_tuples(
        self, account, import_record, parser
    ):
        # 2 QFX rows on 2025-01-01, 2 QFX rows on 2025-01-02.
        for d in [date(2025, 1, 1), date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 2)]:
            _qfx_txn(
                account,
                posted_date=d,
                description_raw="TFL TRAVEL CH",
                settlement_amount_minor=352,
            )
        tuples = [
            _pdf_tuple(
                posted_date=date(2025, 1, 1),
                description_raw="TFL TRAVEL CH TFL.GOV.UK/CP",
                settlement_amount_minor=352,
                transaction_amount_minor=280,
                transaction_currency="GBP",
                exchange_rate=Decimal("1.257"),
            ),
            _pdf_tuple(
                posted_date=date(2025, 1, 1),
                description_raw="TFL TRAVEL CH TFL.GOV.UK/CP",
                settlement_amount_minor=352,
                transaction_amount_minor=280,
                transaction_currency="GBP",
                exchange_rate=Decimal("1.257"),
            ),
            _pdf_tuple(
                posted_date=date(2025, 1, 2),
                description_raw="TFL TRAVEL CH TFL.GOV.UK/CP",
                settlement_amount_minor=352,
                transaction_amount_minor=280,
                transaction_currency="GBP",
                exchange_rate=Decimal("1.257"),
            ),
            _pdf_tuple(
                posted_date=date(2025, 1, 2),
                description_raw="TFL TRAVEL CH TFL.GOV.UK/CP",
                settlement_amount_minor=352,
                transaction_amount_minor=280,
                transaction_currency="GBP",
                exchange_rate=Decimal("1.257"),
            ),
        ]

        for t in tuples:
            outcome = parser._enrich_one(t, account, import_record)
            assert outcome == "enriched"

        # All four QFX rows should be ENRICHED, none left as PARSED.
        assert Transaction.objects.filter(
            account=account, status=Transaction.Status.ENRICHED
        ).count() == 4
        assert Transaction.objects.filter(
            account=account, status=Transaction.Status.PARSED
        ).count() == 0


class TestSyntheticRow:
    def test_no_match_creates_synthetic_needs_review(
        self, account, import_record, parser
    ):
        # Empty DB — no QFX rows match the tuple.
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),
            description_raw="MYSTERY CHARGE",
            settlement_amount_minor=1234,
        )

        outcome = parser._enrich_one(tup, account, import_record)

        assert outcome == "synthetic"
        row = Transaction.objects.get(description_raw="MYSTERY CHARGE")
        assert row.status == Transaction.Status.NEEDS_REVIEW
        assert row.source_transaction_id is None
        assert row.account_id == account.id
        assert row.statement_import_id == import_record.id
        assert row.hash_dedupe  # computed by the base_hash-sequence scheme

    def test_synthetic_rows_are_idempotent(self, account, import_record, parser):
        tup = _pdf_tuple(
            posted_date=date(2025, 1, 10),
            description_raw="MYSTERY CHARGE",
            settlement_amount_minor=1234,
        )

        # Re-run the full PDF walk: each call to _enrich_one uses the parser's
        # hash_counts, which reset per parser instance. A fresh parser instance
        # simulates a fresh run.
        parser1 = ChaseCreditPDFParser(statement_path="/tmp/fake.pdf")
        parser1._enrich_one(tup, account, import_record)
        parser2 = ChaseCreditPDFParser(statement_path="/tmp/fake.pdf")
        parser2._enrich_one(tup, account, import_record)

        # Exactly one synthetic row — the second run should upsert via hash_dedupe.
        assert Transaction.objects.filter(description_raw="MYSTERY CHARGE").count() == 1


class TestPostLoopSweep:
    def test_in_period_parsed_rows_flipped_to_needs_review(
        self, account, import_record, parser
    ):
        # Three QFX rows in-period, plus one out-of-period control.
        in1 = _qfx_txn(
            account, posted_date=date(2025, 1, 10), description_raw="A",
            settlement_amount_minor=100,
        )
        in2 = _qfx_txn(
            account, posted_date=date(2025, 1, 15), description_raw="B",
            settlement_amount_minor=200,
        )
        in3 = _qfx_txn(
            account, posted_date=date(2025, 1, 20), description_raw="C",
            settlement_amount_minor=300,
        )
        out_of_period = _qfx_txn(
            account, posted_date=date(2025, 2, 5), description_raw="D",
            settlement_amount_minor=400,
        )

        updated_count = parser._flag_uncovered_in_period(account, import_record)

        assert updated_count == 3
        in1.refresh_from_db()
        in2.refresh_from_db()
        in3.refresh_from_db()
        out_of_period.refresh_from_db()
        assert in1.status == Transaction.Status.NEEDS_REVIEW
        assert in2.status == Transaction.Status.NEEDS_REVIEW
        assert in3.status == Transaction.Status.NEEDS_REVIEW
        assert out_of_period.status == Transaction.Status.PARSED  # untouched

    def test_enriched_rows_not_reverted(self, account, import_record, parser):
        already = _qfx_txn(
            account, posted_date=date(2025, 1, 10), description_raw="A",
            settlement_amount_minor=100, status=Transaction.Status.ENRICHED,
        )

        parser._flag_uncovered_in_period(account, import_record)

        already.refresh_from_db()
        assert already.status == Transaction.Status.ENRICHED
