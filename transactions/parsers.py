import abc
import hashlib
import io
from datetime import datetime, timedelta
from decimal import Decimal
import pdfplumber
import re
import ofxparse
from ofxparse.ofxparse import AccountType as OfxAccountType
from .models import Account, StatementImport, Transaction


class StatementParser(abc.ABC):
    """An abstract base class for all statement parsers."""

    parser_name = None

    def __init__(self, statement_path: str, stdout=None, style=None):
        self.statement_path = statement_path
        self.stdout = stdout
        self.style = style
        self.hash_counts = {}

    @abc.abstractmethod
    def parse(self):
        """
        Parses the statement file and saves Transaction objects to the database.
        This method must be implemented by all subclasses.
        """
        raise NotImplementedError

def get_parser_choices():
    """Dynamically finds all available parser classes."""
    # This function looks for all classes that inherit from StatementParser
    # and returns a list of their unique `parser_name` attributes.
    return [
        subclass.parser_name
        for subclass in StatementParser.__subclasses__()
        if hasattr(subclass, 'parser_name') and subclass.parser_name
    ]



class SimplePDFParser(StatementParser):
    """A basic parser for simple, single-line transaction tables in PDFs."""
    parser_name = "simple_pdf"

    def parse(self):
        with pdfplumber.open(self.statement_path) as pdf:
            for page in pdf.pages:
                table = page.extract_table()
                if table is None:
                    continue

                # Skip header row
                for row in table[1:]:
                    try:
                        posted_date = datetime.strptime(row[0], "%d/%m/%Y").date()
                        description = row[1].strip()
                        amount = int(float(row[2]) * 100)

                        # For this simple parser, we'll make some assumptions
                        transaction_type = Transaction.TransactionType.DEBIT if amount > 0 else Transaction.TransactionType.CREDIT
                        settlement_currency = "GBP"

                        hash_input = f"{posted_date}{amount}{description}{settlement_currency}".encode()
                        hash_dedupe = hashlib.sha256(hash_input).hexdigest()

                        Transaction.objects.update_or_create(
                            hash_dedupe=hash_dedupe,
                            defaults={
                                "posted_date": posted_date,
                                "description_raw": description,
                                "transaction_type": transaction_type,
                                "settlement_amount_minor": abs(amount),
                                "settlement_currency": settlement_currency,
                                "source_file": self.statement_path,
                            },
                        )
                    except (ValueError, TypeError, IndexError) as e:
                        print(f"Skipping malformed row: {row}. Error: {e}")
                        continue


class ChaseCreditPDFParser(StatementParser):
    """
    Parses Chase credit card PDF statements.

    The PDF no longer creates Transaction rows directly. It is the
    FX-enrichment layer for Chase: it matches PDF rows against existing
    QFX-sourced Transactions and attaches the foreign-currency detail
    (transaction_amount_minor, transaction_currency, exchange_rate) that
    the QFX format strips out.

    Mismatches are surfaced as status=NEEDS_REVIEW rather than silently
    dropped.
    """
    parser_name = "chase_credit_pdf"

    CURRENCY_MAP = {
        "POUND STERLING": "GBP",
        "EURO": "EUR",
        "USD": "USD",  # Explicitly map USD as well
        # Add other currencies here as needed
    }

    # --- Line-matching regex for the transaction walker -----------------
    # A line that STARTS a transaction block: date + description + amount.
    START_TRANSACTION_PATTERN = re.compile(r'^(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})$')
    # A line that contains only a post-date.
    POST_DATE_ONLY_PATTERN = re.compile(r'^(\d{2}/\d{2})$')
    # A line that contains a post-date followed by a currency name (for foreign txns).
    POST_DATE_CURRENCY_PATTERN = re.compile(r'^(\d{2}/\d{2})\s+([A-Z\s]+)$')
    # A line containing only FX rate details, e.g. "6.00 X 1.246666666 (EXCHG RATE)".
    FX_RATE_PATTERN = re.compile(r'^([\d,]+\.\d{2})\s+X\s+([\d.]+)\s+\(EXCHG RATE\)$')
    # Section headers (tolerant of OCR-style repeated chars).
    ACCOUNT_ACTIVITY_HEADER_PATTERN = re.compile(r'^A+C+O+U+N+T+\s+A+C+T+I+V+I+T+Y+', re.IGNORECASE)
    PAYMENTS_CREDITS_HEADER_PATTERN = re.compile(r'^P+A+Y+M+E+N+T+S+\s+A+N+D+\s+O+T+H+E+R+\s+C+R+E+D+I+T+S+', re.IGNORECASE)
    PURCHASE_HEADER_PATTERN = re.compile(r'^P+U+R+C+H+A+S+E+', re.IGNORECASE)
    TRANSACTION_TABLE_HEADER_PATTERN = re.compile(r'Date of\s+Transaction\s+Merchant Name or Transaction Description\s+\$\s*Amount', re.IGNORECASE)
    # Recurring page headers/footers to ignore.
    PAGE_HEADER_PATTERNS = [
        re.compile(r'M+a+n+a+g+e+e+\s+y+o+u+r+r+\s+a+c+c+o+u+n+t+', re.IGNORECASE),
        re.compile(r'www\.chase\.com/cardhelp', re.IGNORECASE),
        re.compile(r'ACCOUNT\s+ACTIVITY\s+\(CONTINUED\)', re.IGNORECASE),
        re.compile(r'^\d{7}\s+FIS\d+\s+D\s+\d+\s+Y\s+\d+\s+\d+\s+\d{2}/\d{2}/\d{2}\s+Page', re.IGNORECASE),
        re.compile(r'VYOM VATS Page\d+\s+of\s+\d+\s+Statement Date:\s+\d{2}/\d{2}/\d{2}', re.IGNORECASE),
    ]
    # Stop transaction parsing on this marker (YTD totals block).
    STOP_PARSING_PATTERN = re.compile(r'^\d{4}\s+Totals\s+Year-to-Date', re.IGNORECASE)

    # Account-number marker on the first page: "Account Number: XXXX XXXX XXXX NNNN".
    CARD_LAST4_PATTERN = re.compile(
        r'Account\s*Number[:\s]+(?:[X\d]{4}\s+){3}(\d{4})',
        re.IGNORECASE,
    )

    # Statement-period marker: "Opening/Closing Date MM/DD/YY - MM/DD/YY".
    STATEMENT_PERIOD_PATTERN = re.compile(
        r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s+-\s+(\d{2}/\d{2}/\d{2})"
    )

    # Descriptions containing this marker are dropped wholesale (travel credits
    # appear in the PDF but are separate from normal purchase/payment rows).
    SKIP_DESCRIPTION_MARKER = "TRAVEL CREDIT"

    # The PDF's date column and QFX's DTPOSTED disagree by a variable amount.
    # For domestic (USD) rows the PDF shows only the transaction date (no post
    # date column), and the QFX DTPOSTED is 1-3 days later. For foreign rows
    # the PDF does have a post-date column, but it can still be 0-1 days
    # earlier than the QFX DTPOSTED. In both cases the PDF is never later
    # than the QFX, so we match against [PDF_date, PDF_date + window].
    POSTING_LAG_DAYS = 4

    def _get_statement_period(self, pdf):
        """
        Finds the statement period (start and end date) from the PDF.
        This is critical for correctly assigning years to transactions.
        It prioritizes the "Opening/Closing Date" on the first page.
        """
        first_page_text = pdf.pages[0].extract_text()
        match = self.STATEMENT_PERIOD_PATTERN.search(first_page_text)
        if match:
            start_date_str, end_date_str = match.groups()
            start_date = datetime.strptime(start_date_str, "%m/%d/%y").date()
            end_date = datetime.strptime(end_date_str, "%m/%d/%y").date()
            return start_date, end_date

        # Fall back to searching the whole document.
        for page in pdf.pages:
            match = self.STATEMENT_PERIOD_PATTERN.search(page.extract_text())
            if match:
                start_date_str, end_date_str = match.groups()
                start_date = datetime.strptime(start_date_str, "%m/%d/%y").date()
                end_date = datetime.strptime(end_date_str, "%m/%d/%y").date()
                return start_date, end_date

        return None

    def _determine_transaction_year(self, trans_month, trans_day, start_date, end_date):
        """
        Determines the correct year for a transaction based on its month
        and the statement's start/end period.
        """
        if start_date.year == end_date.year:
            return start_date.year

        # Statement straddles a year boundary.
        if trans_month >= start_date.month:
            return start_date.year
        else:
            return end_date.year

    def parse(self):
        """
        Parse a Chase credit card PDF and enrich existing QFX-sourced
        transactions with the foreign-currency detail the QFX strips out.

        Does NOT create transactions under the legacy hash-based scheme.
        When the PDF contains a row with no matching QFX transaction, a
        synthetic NEEDS_REVIEW row is created so nothing is lost.
        """
        with pdfplumber.open(self.statement_path) as pdf:
            # Phase A: statement period.
            statement_period = self._get_statement_period(pdf)
            if not statement_period:
                raise ValueError(
                    "Could not determine statement period from 'Opening/Closing Date'. Cannot proceed."
                )
            start_date, end_date = statement_period
            self._log_notice(f"Statement period: {start_date} to {end_date}")

            # Phase B: resolve the Account this PDF belongs to.
            account = self._find_account_from_pdf(pdf)
            self._log_notice(f"Matched account: {account}")

            # Phase C: create the StatementImport for this parse run.
            import_record = StatementImport.objects.create(
                account=account,
                source_file=self.statement_path,
                parser_name=self.parser_name,
                period_start=start_date,
                period_end=end_date,
            )

            # Phase D: walk the PDF and collect per-transaction tuples.
            tuples = self._collect_transaction_tuples(pdf, start_date, end_date)
            self._log_notice(f"Collected {len(tuples)} transaction tuple(s) from PDF.")

        # Phase E: enrich each tuple against existing QFX rows.
        counters = {'enriched': 0, 'ambiguous': 0, 'synthetic': 0}
        for tup in tuples:
            outcome = self._enrich_one(tup, account, import_record)
            counters[outcome] = counters.get(outcome, 0) + 1

        # Phase F: flag in-period PARSED rows the PDF didn't account for.
        unmatched_count = self._flag_uncovered_in_period(account, import_record)

        # Update the import's count to reflect all rows this PDF touched.
        import_record.transaction_count = (
            counters['enriched'] + counters['ambiguous'] + counters['synthetic']
        )
        import_record.save(update_fields=['transaction_count'])

        self._log_success(
            f"PDF enrichment complete for {account}: "
            f"{counters['enriched']} enriched, "
            f"{counters['ambiguous']} flagged ambiguous, "
            f"{counters['synthetic']} synthetic (no QFX match), "
            f"{unmatched_count} QFX row(s) in period without a PDF match."
        )

    # ---- PDF walker --------------------------------------------------------

    def _collect_transaction_tuples(self, pdf, start_date, end_date):
        """
        Walk the PDF's transaction tables and return a list of tuple dicts.

        This is the original multi-line block walker from the legacy parser,
        refactored to emit tuples instead of writing to the database. The
        walker logic itself (section identification, header/footer stripping,
        multi-line FX blocks) is unchanged in behavior.
        """
        tuples = []
        current_transaction_data = {}
        description_lines = []
        parsing_transactions_section = False

        for i, page in enumerate(pdf.pages):
            # Page 1 (0-indexed) is the legal boilerplate page, skip it.
            if i == 1:
                self._log_notice("Skipping Page 2 (index 1) as it contains boilerplate text.")
                continue

            page_text = page.extract_text() or ''
            # Join the known broken header line ("Date of" / "Transaction" split).
            page_text = re.sub(r'Date of\s*\n\s*Transaction', 'Date of Transaction', page_text, flags=re.IGNORECASE)
            lines = page_text.split('\n')

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # End-of-transaction-section marker (YTD totals block).
                if self.STOP_PARSING_PATTERN.search(line):
                    self._log_notice(
                        f"Found 'Totals Year-to-Date' on page {i + 1}; stopping transaction parsing for this page."
                    )
                    if current_transaction_data:
                        self._emit_tuple(tuples, current_transaction_data, description_lines, start_date, end_date)
                        current_transaction_data, description_lines = {}, []
                    parsing_transactions_section = False
                    break

                # Skip known page headers/footers.
                if any(p.search(line) for p in self.PAGE_HEADER_PATTERNS):
                    continue

                # Skip the transaction-table column header.
                if parsing_transactions_section and self.TRANSACTION_TABLE_HEADER_PATTERN.search(line):
                    continue

                # Haven't entered ACCOUNT ACTIVITY yet.
                if not parsing_transactions_section:
                    if self.ACCOUNT_ACTIVITY_HEADER_PATTERN.search(line):
                        self._log_notice(f"Found 'ACCOUNT ACTIVITY' section on page {i + 1}.")
                        parsing_transactions_section = True
                    continue

                # Sub-section boundaries: flush any pending block first.
                if self.PAYMENTS_CREDITS_HEADER_PATTERN.search(line):
                    if current_transaction_data:
                        self._emit_tuple(tuples, current_transaction_data, description_lines, start_date, end_date)
                        current_transaction_data, description_lines = {}, []
                    continue
                if self.PURCHASE_HEADER_PATTERN.search(line):
                    if current_transaction_data:
                        self._emit_tuple(tuples, current_transaction_data, description_lines, start_date, end_date)
                        current_transaction_data, description_lines = {}, []
                    continue

                # New transaction block starts.
                start_match = self.START_TRANSACTION_PATTERN.match(line)
                if start_match:
                    if current_transaction_data:
                        self._emit_tuple(tuples, current_transaction_data, description_lines, start_date, end_date)
                    current_transaction_data = {
                        'transaction_date_str': start_match.group(1),
                        'settlement_amount_str': start_match.group(3),
                    }
                    description_lines = [start_match.group(2).strip()]
                    continue

                # Continuation lines for the current transaction block.
                if current_transaction_data:
                    m_post_cur = self.POST_DATE_CURRENCY_PATTERN.match(line)
                    m_post_only = self.POST_DATE_ONLY_PATTERN.match(line)
                    m_fx = self.FX_RATE_PATTERN.match(line)
                    if m_post_cur:
                        current_transaction_data['posted_date_str'] = m_post_cur.group(1)
                        current_transaction_data['transaction_currency'] = m_post_cur.group(2).strip()
                    elif m_post_only:
                        current_transaction_data['posted_date_str'] = m_post_only.group(1)
                    elif m_fx:
                        current_transaction_data['transaction_amount_str'] = m_fx.group(1)
                        current_transaction_data['exchange_rate_str'] = m_fx.group(2)

        # End-of-document flush.
        if current_transaction_data:
            self._emit_tuple(tuples, current_transaction_data, description_lines, start_date, end_date)

        return tuples

    def _emit_tuple(self, tuples, data, description_lines, start_date, end_date):
        """Convert a raw block-dict into a normalized tuple and append to `tuples`."""
        tup = self._extract_transaction_tuple(data, description_lines, start_date, end_date)
        if tup is not None:
            tuples.append(tup)

    def _extract_transaction_tuple(self, data, description_lines, start_date, end_date):
        """
        Turn a raw transaction-block dict into a normalized tuple. Returns
        None if the block should be skipped (e.g. TRAVEL CREDIT rows), or
        if parsing fails.

        The tuple keys are:
            posted_date, transaction_date, description_raw, transaction_type,
            settlement_amount_minor, settlement_currency,
            transaction_amount_minor, transaction_currency, exchange_rate.

        FX fields are None for same-currency (USD) transactions.
        """
        description = " ".join(description_lines).strip()
        if self.SKIP_DESCRIPTION_MARKER in description.upper():
            self._log_notice(
                f"Skipping transaction with '{self.SKIP_DESCRIPTION_MARKER}' in description: {description!r}"
            )
            return None

        try:
            trans_month, trans_day = map(int, data['transaction_date_str'].split('/'))
            trans_year = self._determine_transaction_year(trans_month, trans_day, start_date, end_date)
            transaction_date = datetime(trans_year, trans_month, trans_day).date()

            post_date_str = data.get('posted_date_str', data['transaction_date_str'])
            post_month, post_day = map(int, post_date_str.split('/'))
            post_year = self._determine_transaction_year(post_month, post_day, start_date, end_date)
            posted_date = datetime(post_year, post_month, post_day).date()

            if 'settlement_amount_str' not in data:
                raise ValueError("Settlement amount not found for transaction block.")
            # Use Decimal, not float, for monetary parsing — float("19.99") * 100
            # produces 1998.9999..., and int() would round that down to 1998.
            settlement_decimal = Decimal(data['settlement_amount_str'].replace('$', '').replace(',', ''))
            settlement_amount_raw = int((settlement_decimal * 100).to_integral_value(rounding='ROUND_HALF_UP'))
            # On the PDF, purchases are positive and credits (refunds, payments)
            # are negative. This is the opposite of QFX convention — see the
            # QFX parser for the other side. For enrichment matching, what
            # matters is that transaction_type agrees with the QFX side.
            transaction_type = (
                Transaction.TransactionType.DEBIT if settlement_amount_raw > 0
                else Transaction.TransactionType.CREDIT
            )
            settlement_amount_minor = abs(settlement_amount_raw)
            settlement_currency = "USD"

            transaction_amount_minor = None
            transaction_currency = None
            exchange_rate = None
            if (
                'transaction_amount_str' in data
                and 'transaction_currency' in data
                and 'exchange_rate_str' in data
            ):
                transaction_amount_decimal = Decimal(
                    data['transaction_amount_str'].replace('$', '').replace(',', '')
                )
                transaction_amount_minor = int(
                    (transaction_amount_decimal * 100).to_integral_value(rounding='ROUND_HALF_UP')
                )
                full_currency_name = data['transaction_currency']
                transaction_currency = self.CURRENCY_MAP.get(
                    full_currency_name, full_currency_name[:3]
                )
                exchange_rate = Decimal(data['exchange_rate_str'])

            return {
                'posted_date': posted_date,
                'transaction_date': transaction_date,
                'description_raw': description,
                'transaction_type': transaction_type,
                'settlement_amount_minor': settlement_amount_minor,
                'settlement_currency': settlement_currency,
                'transaction_amount_minor': transaction_amount_minor,
                'transaction_currency': transaction_currency,
                'exchange_rate': exchange_rate,
            }
        except (ValueError, TypeError, IndexError, AttributeError, KeyError) as e:
            self._log_error(
                f"Skipping malformed transaction block: {data}. Description: {description_lines}. Error: {e}"
            )
            return None

    # ---- Account resolution ------------------------------------------------

    def _find_account_from_pdf(self, pdf):
        """Resolve the Account for this PDF via the card's last-4 digits."""
        last4 = None
        for page in pdf.pages:
            text = page.extract_text() or ''
            match = self.CARD_LAST4_PATTERN.search(text)
            if match:
                last4 = match.group(1)
                break

        if not last4:
            raise ValueError(
                "Could not determine card last-4 from PDF. Expected a line like "
                "'Account Number: XXXX XXXX XXXX NNNN'."
            )

        qs = Account.objects.filter(
            institution="Chase",
            account_identifier__endswith=last4,
            account_type=Account.AccountType.CREDIT_CARD,
        )
        count = qs.count()
        if count == 0:
            raise ValueError(
                f"No Chase account found matching last-4 '{last4}'. "
                "Import the matching QFX file first so the account is auto-created."
            )
        if count > 1:
            raise ValueError(
                f"Multiple Chase accounts match last-4 '{last4}'. Cannot disambiguate; "
                "please edit account_identifier values in the admin."
            )
        return qs.first()

    # ---- Enrichment matching -----------------------------------------------

    def _enrich_one(self, tup, account, import_record):
        """
        Match a PDF tuple against existing Transactions in `account` and
        either enrich (for foreign rows) or acknowledge (for domestic rows).

        Matching uses a forward date window rather than exact equality
        because the PDF's post-date column can run 0-3 days behind QFX's
        DTPOSTED for the same transaction (Chase's two sources disagree
        on posting date by a variable lag). The PDF is never later than
        the QFX, so the window is [PDF_date, PDF_date + POSTING_LAG_DAYS].

        Foreign tuples (those carrying FX data) get full enrichment:
        FX fields written, status → ENRICHED. Domestic tuples get
        acknowledgment only: status → ENRICHED, statement_import linked,
        no FX fields written. Both paths fall back to a synthetic
        NEEDS_REVIEW row if nothing matches in the window — that's the
        "PDF saw something QFX didn't" signal required by the
        lossless-processing principle.

        When the window yields multiple candidates, the parser tries:

        1. Description substring (bidirectional, case-insensitive). If this
           narrows to exactly one, enrich that one.
        2. Uniform-cluster handling. If all candidates share the same
           description (e.g. four TFL taps on the same day), assign the
           current tuple to the first un-enriched candidate. Subsequent
           tuples in the same cluster each pick the next un-enriched
           candidate. This scrambles per-row PDF→QFX pairings within the
           cluster but attaches the correct FX-data set to the correct
           QFX-row set — aggregates stay correct, and no FX data is lost.
        3. Otherwise (genuinely different descriptions), flag all
           candidates as NEEDS_REVIEW.

        Returns one of: 'enriched', 'ambiguous', 'synthetic'.
        """
        window_end = tup['posted_date'] + timedelta(days=self.POSTING_LAG_DAYS)
        candidates = list(self._base_candidates_qs(tup, account).filter(
            posted_date__gte=tup['posted_date'],
            posted_date__lte=window_end,
        ))

        if len(candidates) == 1:
            self._attach_fx(candidates[0], tup, import_record)
            return 'enriched'

        if len(candidates) > 1:
            # Disambiguate via bidirectional case-insensitive substring match.
            desc = (tup['description_raw'] or '').lower()
            narrowed = [
                c for c in candidates
                if desc and c.description_raw and (
                    desc in c.description_raw.lower()
                    or c.description_raw.lower() in desc
                )
            ]
            if len(narrowed) == 1:
                self._attach_fx(narrowed[0], tup, import_record)
                return 'enriched'

            # Uniform cluster: multiple QFX candidates with identical
            # description. This is the common "4 TFL taps on the same day"
            # case — the FX data on the PDF is valid for whichever candidate
            # we pick, and successive PDF tuples for the same cluster will
            # each pick the next un-enriched candidate. See the rationale in
            # design.md under "Enrichment matching".
            descs = {(c.description_raw or '').strip() for c in candidates}
            if len(descs) == 1:
                unenriched = [
                    c for c in candidates
                    if c.status != Transaction.Status.ENRICHED
                ]
                if unenriched:
                    self._attach_fx(unenriched[0], tup, import_record)
                    return 'enriched'
                # All candidates already enriched by earlier tuples in this
                # run; the current tuple is a duplicate of data we already
                # attached. Treat it as an acknowledgment, not a mismatch.
                return 'enriched'

            # Genuinely ambiguous (different descriptions, can't pick one).
            self._log_notice(
                f"Ambiguous PDF match for {tup['posted_date']} "
                f"${tup['settlement_amount_minor'] / 100:.2f} "
                f"({tup['description_raw']!r}): {len(candidates)} candidates."
            )
            for c in candidates:
                c.status = Transaction.Status.NEEDS_REVIEW
                c.save(update_fields=['status'])
            return 'ambiguous'

        # No match — create a synthetic NEEDS_REVIEW row, preserving data.
        self._create_synthetic(tup, account, import_record)
        return 'synthetic'

    def _base_candidates_qs(self, tup, account):
        """Candidate queryset shared by the foreign and domestic match paths."""
        return Transaction.objects.filter(
            account=account,
            settlement_amount_minor=tup['settlement_amount_minor'],
            settlement_currency=tup['settlement_currency'],
            transaction_type=tup['transaction_type'],
        ).order_by('posted_date', 'id')

    def _attach_fx(self, txn, tup, import_record):
        """
        Update `txn` using the PDF tuple, flip it to ENRICHED, and link it
        to this PDF StatementImport.

        FX fields (transaction_amount_minor, transaction_currency,
        exchange_rate) are only written when the tuple actually has them —
        i.e. for foreign transactions. Domestic tuples pass through this
        function as a pure acknowledgment with no FX side effect.
        """
        if tup.get('transaction_amount_minor') is not None:
            txn.transaction_amount_minor = tup['transaction_amount_minor']
            txn.transaction_currency = tup['transaction_currency']
            txn.exchange_rate = tup['exchange_rate']
            # The transaction_date only applies to foreign rows (where the PDF
            # carries it as a separate column). For domestic rows the PDF's
            # single date is already reflected in the QFX's posted_date, so
            # leaving transaction_date null for those rows is correct.
            if tup.get('transaction_date') is not None:
                txn.transaction_date = tup['transaction_date']
        txn.statement_import = import_record
        txn.status = Transaction.Status.ENRICHED
        txn.save()

    def _create_synthetic(self, tup, account, import_record):
        """
        Create (or re-touch) a synthetic NEEDS_REVIEW row for a PDF tuple
        that has no corresponding QFX transaction.

        Uses the legacy base_hash-sequence scheme on `hash_dedupe` to keep
        re-runs of the same PDF idempotent (no bank-assigned ID is available).
        """
        hash_dedupe = self._compute_synthetic_hash_dedupe(tup)
        Transaction.objects.update_or_create(
            hash_dedupe=hash_dedupe,
            defaults={
                'account': account,
                'source_transaction_id': None,
                'status': Transaction.Status.NEEDS_REVIEW,
                'statement_import': import_record,
                'posted_date': tup['posted_date'],
                'transaction_date': tup['transaction_date'],
                'description_raw': tup['description_raw'],
                'transaction_type': tup['transaction_type'],
                'settlement_amount_minor': tup['settlement_amount_minor'],
                'settlement_currency': tup['settlement_currency'],
                'transaction_amount_minor': tup.get('transaction_amount_minor'),
                'transaction_currency': tup.get('transaction_currency'),
                'exchange_rate': tup.get('exchange_rate'),
                'source_file': self.statement_path,
            },
        )

    def _compute_synthetic_hash_dedupe(self, tup):
        """Legacy `base_hash-sequence` scheme for synthetic PDF-only rows."""
        base_input = (
            f"{tup['posted_date']}{tup['settlement_amount_minor']}"
            f"{tup['description_raw']}{tup['settlement_currency']}"
        ).encode()
        base_hash = hashlib.sha256(base_input).hexdigest()
        sequence = self.hash_counts.get(base_hash, 0)
        self.hash_counts[base_hash] = sequence + 1
        return f"{base_hash}-{sequence}"

    # ---- Post-loop sweep ---------------------------------------------------

    def _flag_uncovered_in_period(self, account, import_record):
        """
        Flip in-period Transactions that are still PARSED (i.e. QFX-sourced
        but not covered by any PDF tuple this run) to NEEDS_REVIEW.
        Returns the number of rows updated.
        """
        if import_record.period_start is None or import_record.period_end is None:
            return 0
        return Transaction.objects.filter(
            account=account,
            posted_date__gte=import_record.period_start,
            posted_date__lte=import_record.period_end,
            status=Transaction.Status.PARSED,
        ).update(status=Transaction.Status.NEEDS_REVIEW)

    # ---- Logging helpers ---------------------------------------------------

    def _log_notice(self, msg):
        if self.stdout and self.style:
            self.stdout.write(self.style.NOTICE(msg))
        elif self.stdout:
            self.stdout.write(msg)

    def _log_success(self, msg):
        if self.stdout and self.style:
            self.stdout.write(self.style.SUCCESS(msg))
        elif self.stdout:
            self.stdout.write(msg)

    def _log_error(self, msg):
        if self.stdout and self.style:
            self.stdout.write(self.style.ERROR(msg))
        elif self.stdout:
            self.stdout.write(msg)


class ChaseCreditQFXParser(StatementParser):
    """
    Parses Chase credit card QFX statements.

    QFX is the primary source of Chase credit card data. The parser keys
    transactions on (account, FITID) for deduplication, and preserves any
    foreign-currency enrichment already attached by the PDF parser.
    """
    parser_name = "chase_credit_qfx"

    INSTITUTION_NAME = "Chase"
    EXPECTED_FID = "10898"
    EXPECTED_ORG = "B1"
    EXPECTED_INTU_BID = "10898"

    # Preserved on updates so PDF enrichment isn't clobbered by a QFX re-import.
    FX_FIELDS = ('transaction_amount_minor', 'transaction_currency', 'exchange_rate')

    def parse(self):
        ofx = self._load_ofx()
        self._verify_chase_signon(ofx)
        account_obj, ccstmt = self._get_credit_card_statement(ofx)

        account = self._get_or_create_account(account_obj, ccstmt)
        import_record = self._create_statement_import(account, ccstmt)

        created, updated = 0, 0
        for txn in ccstmt.transactions:
            if self._upsert_transaction(txn, account, import_record):
                created += 1
            else:
                updated += 1

        import_record.transaction_count = created + updated
        import_record.save(update_fields=['transaction_count'])

        self._log_success(
            f"QFX parse complete for {account}: {created} created, {updated} updated."
        )

    # ---- helpers -----------------------------------------------------------

    def _load_ofx(self):
        """
        Read the QFX file and hand it to ofxparse.

        Chase QFX files seen in the wild have two quirks that break a naive
        `ofxparse.OfxParser.parse(open(path, 'rb'))`:

        1. A leading newline before the OFX header block, which makes
           ofxparse's header parser stop before reading any headers and
           fall back to ASCII decoding.
        2. Stray bytes (often UTF-8 encoding of the replacement character
           U+FFFD) embedded in <NAME> fields despite the file declaring
           CHARSET:1252.

        Pre-processing the raw bytes — strip leading whitespace, then
        decode/re-encode as cp1252 with errors='replace' — fixes both in
        one pass without patching ofxparse.
        """
        with open(self.statement_path, 'rb') as f:
            raw = f.read()
        cleaned = (
            raw.lstrip()
            .decode('cp1252', errors='replace')
            .encode('cp1252', errors='replace')
        )
        return ofxparse.OfxParser.parse(io.BytesIO(cleaned))

    def _verify_chase_signon(self, ofx):
        """Raise if the file doesn't look like a Chase QFX."""
        signon = ofx.signon
        fid = getattr(signon, 'fi_fid', None) or ''
        org = getattr(signon, 'fi_org', None) or ''
        intu_bid = getattr(signon, 'intu_bid', None) or ''

        if (
            fid != self.EXPECTED_FID
            or org != self.EXPECTED_ORG
            or intu_bid != self.EXPECTED_INTU_BID
        ):
            raise ValueError(
                "File does not look like a Chase QFX statement. "
                f"Expected FID={self.EXPECTED_FID}, ORG={self.EXPECTED_ORG}, "
                f"INTU.BID={self.EXPECTED_INTU_BID}; "
                f"got FID={fid!r}, ORG={org!r}, INTU.BID={intu_bid!r}."
            )

    def _get_credit_card_statement(self, ofx):
        """Find and return (account_obj, statement) for the credit-card account."""
        if not ofx.accounts:
            raise ValueError("QFX file contains no accounts.")

        for account_obj in ofx.accounts:
            if account_obj.type == OfxAccountType.CreditCard:
                return account_obj, account_obj.statement

        types_seen = sorted({a.type for a in ofx.accounts})
        raise ValueError(
            "QFX file contains no credit-card statement. "
            f"Account types seen: {types_seen}. "
            "This parser only handles credit-card QFX files; use a different "
            "parser for bank/checking statements."
        )

    def _get_or_create_account(self, account_obj, ccstmt):
        """Look up or create the Account this QFX file describes."""
        currency = (ccstmt.currency or '').upper()
        account, _ = Account.objects.get_or_create(
            institution=self.INSTITUTION_NAME,
            account_identifier=account_obj.account_id,
            defaults={
                'account_type': Account.AccountType.CREDIT_CARD,
                'default_currency': currency,
            },
        )
        return account

    def _create_statement_import(self, account, ccstmt):
        """Record a new StatementImport row for this parse run."""
        return StatementImport.objects.create(
            account=account,
            source_file=self.statement_path,
            parser_name=self.parser_name,
            period_start=self._to_date(ccstmt.start_date),
            period_end=self._to_date(ccstmt.end_date),
            ledger_balance_minor=self._to_minor(ccstmt.balance),
            available_balance_minor=self._to_minor(ccstmt.available_balance),
        )

    def _upsert_transaction(self, txn, account, import_record):
        """
        Create or update a Transaction keyed on (account, FITID).

        Returns True if a row was created, False if an existing row was
        updated. On update, FX fields and the ENRICHED status are preserved
        so that a QFX re-import doesn't clobber PDF enrichment.
        """
        posted_date = self._to_date(txn.date)
        settlement_minor = abs(self._to_minor(txn.amount))
        trn_type = self._map_trn_type(txn.type)
        description = txn.payee or ''

        defaults = {
            'posted_date': posted_date,
            'description_raw': description,
            'transaction_type': trn_type,
            'settlement_amount_minor': settlement_minor,
            'settlement_currency': account.default_currency,
            'status': Transaction.Status.PARSED,
            'statement_import': import_record,
            'source_file': self.statement_path,
        }

        obj, created = Transaction.objects.get_or_create(
            account=account,
            source_transaction_id=txn.id,
            defaults=defaults,
        )

        if created:
            return True

        # Update non-FX fields on an existing row. FX fields and ENRICHED
        # status are intentionally preserved.
        obj.posted_date = posted_date
        obj.description_raw = description
        obj.transaction_type = trn_type
        obj.settlement_amount_minor = settlement_minor
        obj.settlement_currency = account.default_currency
        obj.statement_import = import_record
        obj.source_file = self.statement_path
        if obj.status != Transaction.Status.ENRICHED:
            obj.status = Transaction.Status.PARSED
        obj.save()
        return False

    @staticmethod
    def _to_minor(amount):
        """Convert a Decimal amount to an integer number of minor units."""
        if amount is None:
            return None
        return int((Decimal(amount) * 100).to_integral_value(rounding='ROUND_HALF_UP'))

    @staticmethod
    def _to_date(dt):
        """Return the date portion of a datetime, or None."""
        if dt is None:
            return None
        if hasattr(dt, 'date'):
            return dt.date()
        return dt

    @staticmethod
    def _map_trn_type(raw_type):
        """Map ofxparse's lowercase TRNTYPE string to the Transaction enum."""
        if (raw_type or '').lower() == 'credit':
            return Transaction.TransactionType.CREDIT
        return Transaction.TransactionType.DEBIT

    def _log_success(self, msg):
        if self.stdout and self.style:
            self.stdout.write(self.style.SUCCESS(msg))
        elif self.stdout:
            self.stdout.write(msg)
