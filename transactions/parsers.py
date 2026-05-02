import abc
import hashlib
import io
from datetime import datetime
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
    """Parses Chase credit card PDF statements."""
    parser_name = "chase_credit_pdf"
    CURRENCY_MAP = {
        "POUND STERLING": "GBP",
        "EURO": "EUR",
        "USD": "USD", # Explicitly map USD as well
        # Add other currencies here as needed
    }

    def _get_statement_period(self, pdf: pdfplumber.PDF) -> tuple[datetime.date, datetime.date] | None:
        """
        Finds the statement period (start and end date) from the PDF.
        This is critical for correctly assigning years to transactions.
        It prioritizes the "Opening/Closing Date" on the first page.
        """
        # Regex to find "Opening/Closing Date MM/DD/YY - MM/DD/YY"
        period_regex = re.compile(r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s+-\s+(\d{2}/\d{2}/\d{2})")

        # Check the first page first, as it's the most likely location
        first_page_text = pdf.pages[0].extract_text()
        match = period_regex.search(first_page_text)
        if match:
            start_date_str, end_date_str = match.groups()
            start_date = datetime.strptime(start_date_str, "%m/%d/%y").date()
            end_date = datetime.strptime(end_date_str, "%m/%d/%y").date()
            return start_date, end_date

        # If not on the first page, search the whole document (less likely)
        for page in pdf.pages:
            match = period_regex.search(page.extract_text())
            if match:
                start_date_str, end_date_str = match.groups()
                start_date = datetime.strptime(start_date_str, "%m/%d/%y").date()
                end_date = datetime.strptime(end_date_str, "%m/%d/%y").date()
                return start_date, end_date
        
        return None
    
    def _determine_transaction_year(self, trans_month: int, trans_day: int, start_date: datetime.date, end_date: datetime.date) -> int:
        """
        Determines the correct year for a transaction based on its month
        and the statement's start/end period.
        """
        # If the statement period is within the same year
        if start_date.year == end_date.year:
            return start_date.year
        
        # If the statement straddles two years (e.g., Dec-Jan)
        # A transaction month >= start month belongs to the start year
        if trans_month >= start_date.month:
            return start_date.year
        # A transaction month <= end month belongs to the end year
        else:
            return end_date.year

    def parse(self):
        # --- DEFINITIVE REGEX PATTERNS ---
        # A line that STARTS a transaction block. MUST contain date, description, AND amount.
        start_transaction_pattern = re.compile(r'^(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})$')

        # A line that ONLY contains a post-date. This is for rare cases.
        post_date_only_pattern = re.compile(r'^(\d{2}/\d{2})$')
        post_date_currency_pattern = re.compile(r'^(\d{2}/\d{2})\s+([A-Z\s]+)$')

        # Pattern for a line containing only FX rate details (e.g., 6.00 X 1.246666666 (EXCHG RATE))
        fx_rate_pattern = re.compile(r'^([\d,]+\.\d{2})\s+X\s+([\d.]+)\s+\(EXCHG RATE\)$')

        # Patterns for section headers (robust to OCR errors like repeated chars)
        account_activity_header_pattern = re.compile(r'^A+C+O+U+N+T+\s+A+C+T+I+V+I+T+Y+', re.IGNORECASE)
        payments_credits_header_pattern = re.compile(r'^P+A+Y+M+E+N+T+S+\s+A+N+D+\s+O+T+H+E+R+\s+C+R+E+D+I+T+S+', re.IGNORECASE)
        purchase_header_pattern = re.compile(r'^P+U+R+C+H+A+S+E+', re.IGNORECASE)
        transaction_table_header_pattern = re.compile(r'Date of\s+Transaction\s+Merchant Name or Transaction Description\s+\$\s*Amount', re.IGNORECASE)
        # Patterns for recurring page headers/footers to be ignored
        page_header_patterns = [
            re.compile(r'M+a+n+a+g+e+e+\s+y+o+u+r+r+\s+a+c+c+o+u+n+t+', re.IGNORECASE),
            re.compile(r'www\.chase\.com/cardhelp', re.IGNORECASE),
            re.compile(r'ACCOUNT\s+ACTIVITY\s+\(CONTINUED\)', re.IGNORECASE),
            re.compile(r'^\d{7}\s+FIS\d+\s+D\s+\d+\s+Y\s+\d+\s+\d+\s+\d{2}/\d{2}/\d{2}\s+Page', re.IGNORECASE), # Footer line
            re.compile(r'VYOM VATS Page\d+\s+of\s+\d+\s+Statement Date:\s+\d{2}/\d{2}/\d{2}', re.IGNORECASE), # Page footer with name and date
        ]
        # Pattern to stop transaction parsing (e.g., Year-to-Date totals)
        stop_parsing_pattern = re.compile(r'^\d{4}\s+Totals\s+Year-to-Date', re.IGNORECASE)

        with pdfplumber.open(self.statement_path) as pdf:
            statement_period = self._get_statement_period(pdf)
            if not statement_period:
                raise ValueError("Could not determine statement period from 'Opening/Closing Date'. Cannot proceed.")
            
            start_date, end_date = statement_period
            self.stdout.write(self.style.SUCCESS(f"Determined statement period: {start_date} to {end_date}"))
            

            current_transaction_data = {}
            description_lines = []
            parsing_transactions_section = False

            for i, page in enumerate(pdf.pages):
                # Page 1 (0-indexed) is the legal boilerplate page, skip it.
                if i == 1:
                    self.stdout.write(self.style.NOTICE(f"Skipping Page 2 (index 1) as it contains boilerplate text."))
                    # Ensure any pending transaction block is processed before skipping the page
                    continue

                page_text = page.extract_text()
                # Pre-process the page text to join the known broken header line
                # This handles cases where "Date of" is on one line and the rest of the header is on the next.
                page_text = re.sub(r'Date of\s*\n\s*Transaction', 'Date of Transaction', page_text, flags=re.IGNORECASE)

                lines = page_text.split('\n')

                for line in lines:
                    line = line.strip()
                    if not line:
                        continue

                    # Check for end-of-transaction-section markers
                    if stop_parsing_pattern.search(line):
                        self.stdout.write(self.style.NOTICE(f"Found 'Totals Year-to-Date' section on page {i+1}. Stopping transaction parsing for this page."))
                        # Process any pending transaction before stopping
                        if current_transaction_data:
                            self._process_transaction_block(current_transaction_data, description_lines, start_date, end_date)
                            current_transaction_data = {}
                            description_lines = []
                        parsing_transactions_section = False # Stop processing transactions
                        break # Exit inner loop for this page, move to next page

                    # Skip any line that matches a known page header or footer pattern
                    if any(p.search(line) for p in page_header_patterns):
                        self.stdout.write(self.style.NOTICE(f"Skipping known page header/footer: '{line}'"))
                        continue

                    # If we are in the transactions section, skip the table header
                    if parsing_transactions_section and transaction_table_header_pattern.search(line):
                        self.stdout.write(self.style.NOTICE(f"Skipping transaction table header: '{line}'"))
                        continue

                    # --- Section Identification Logic ---
                    # Only start processing transaction lines after "ACCOUNT ACTIVITY"
                    if not parsing_transactions_section:
                        if account_activity_header_pattern.search(line):
                            self.stdout.write(self.style.NOTICE(f"Found 'ACCOUNT ACTIVITY' section on page {i+1}."))
                            parsing_transactions_section = True
                        continue # Skip lines until ACCOUNT ACTIVITY is found

                    # Identify sub-sections (Payments/Credits or Purchases)
                    # Process any pending transaction block before starting a new section
                    if payments_credits_header_pattern.search(line):
                        self.stdout.write(self.style.NOTICE(f"Found 'PAYMENTS AND OTHER CREDITS' sub-section."))
                        if current_transaction_data:
                            self._process_transaction_block(current_transaction_data, description_lines, start_date, end_date)
                            current_transaction_data, description_lines = {}, []
                        continue

                    if purchase_header_pattern.search(line):
                        self.stdout.write(self.style.NOTICE(f"Found 'PURCHASE' sub-section."))
                        if current_transaction_data:
                            self._process_transaction_block(current_transaction_data, description_lines, start_date, end_date)
                            current_transaction_data, description_lines = {}, []
                        continue

                    # --- TRANSACTION BLOCK LOGIC ---
                    start_match = start_transaction_pattern.match(line)
                    if start_match:
                        # If we have a complete previous transaction, process it
                        if current_transaction_data:
                            self._process_transaction_block(current_transaction_data, description_lines, start_date, end_date)
                        
                        # Reset for the new transaction
                        current_transaction_data = {'transaction_date_str': start_match.group(1)}
                        description_lines = [start_match.group(2).strip()]
                        current_transaction_data['settlement_amount_str'] = start_match.group(3)
                        continue # Move to the next line

                    if current_transaction_data: # If we are in the middle of a transaction block
                        post_date_currency_match = post_date_currency_pattern.match(line)
                        post_date_only_match = post_date_only_pattern.match(line)
                        fx_rate_match = fx_rate_pattern.match(line)

                        if post_date_currency_match:
                            current_transaction_data['posted_date_str'] = post_date_currency_match.group(1)
                            current_transaction_data['transaction_currency'] = post_date_currency_match.group(2).strip()
                        elif post_date_only_match:
                            current_transaction_data['posted_date_str'] = post_date_only_match.group(1)
                        elif fx_rate_match:
                            current_transaction_data['transaction_amount_str'] = fx_rate_match.group(1)
                            current_transaction_data['exchange_rate_str'] = fx_rate_match.group(2)
            
            # After all pages, process any remaining transaction block (the last one)
            if current_transaction_data:
                self._process_transaction_block(current_transaction_data, description_lines, start_date, end_date)
    
    def _process_transaction_block(self, data: dict, description_lines: list, start_date: datetime.date, end_date: datetime.date):
        """Helper to process a single transaction block and save it."""
        description = " ".join(description_lines).strip()
        if "TRAVEL CREDIT" in description.upper():
            self.stdout.write(self.style.NOTICE(f"Skipping transaction with 'TRAVEL CREDIT' in description: '{description}'"))
            return

        try:
            trans_month, trans_day = map(int, data['transaction_date_str'].split('/'))
            trans_year = self._determine_transaction_year(trans_month, trans_day, start_date, end_date)
            transaction_date = datetime(trans_year, trans_month, trans_day).date()

            post_date_str = data.get('posted_date_str', data['transaction_date_str'])
            post_month, post_day = map(int, post_date_str.split('/'))
            post_year = self._determine_transaction_year(post_month, post_day, start_date, end_date)
            posted_date = datetime(post_year, post_month, post_day).date()
            
            # Ensure settlement_amount_str is present, it should be from the first line of the block
            if 'settlement_amount_str' not in data:
                raise ValueError("Settlement amount not found for transaction block.")
            settlement_amount = int(float(data['settlement_amount_str'].replace('$', '').replace(',', '')) * 100) # Convert to minor units
            settlement_currency = "USD"
            transaction_type = Transaction.TransactionType.DEBIT if settlement_amount > 0 else Transaction.TransactionType.CREDIT

            transaction_amount_minor = None
            transaction_currency = None
            exchange_rate = None

            if 'transaction_amount_str' in data and 'transaction_currency' in data and 'exchange_rate_str' in data:
                transaction_amount_minor = int(float(data['transaction_amount_str'].replace('$', '').replace(',', '')) * 100)
                full_currency_name = data['transaction_currency']
                transaction_currency = self.CURRENCY_MAP.get(full_currency_name, full_currency_name[:3]) # Fallback to first 3 chars
                exchange_rate = float(data['exchange_rate_str'])

            # --- Sequenced Hashing for Deduplication ---
            # 1. Create a base hash from the non-unique transaction data.
            base_hash_input = f"{posted_date}{settlement_amount}{description}{settlement_currency}".encode()
            base_hash = hashlib.sha256(base_hash_input).hexdigest()

            # 2. Get the sequence number for this base hash for this run.
            sequence = self.hash_counts.get(base_hash, 0)

            # 3. Create the final, unique hash by appending the sequence.
            hash_dedupe = f"{base_hash}-{sequence}"
            self.hash_counts[base_hash] = sequence + 1

            Transaction.objects.update_or_create(
                hash_dedupe=hash_dedupe,
                defaults={
                    "transaction_date": transaction_date,
                    "posted_date": posted_date,
                    "description_raw": description,
                    "transaction_type": transaction_type,
                    "settlement_amount_minor": abs(settlement_amount),
                    "settlement_currency": settlement_currency,
                    "transaction_amount_minor": transaction_amount_minor,
                    "transaction_currency": transaction_currency,
                    "exchange_rate": exchange_rate,
                    "source_file": self.statement_path,
                },
            )
        except (ValueError, TypeError, IndexError, AttributeError, KeyError) as e:
            self.stdout.write(self.style.ERROR(f"Skipping malformed transaction block: {data}. Description: {description_lines}. Error: {e}"))


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