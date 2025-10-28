import abc
import hashlib
from datetime import datetime
import pdfplumber
from .models import Transaction


class StatementParser(abc.ABC):
    """An abstract base class for all statement parsers."""

    parser_name = None

    def __init__(self, statement_path: str):
        self.statement_path = statement_path

    @abc.abstractmethod
    def parse(self):
        """
        Parses the statement file and saves Transaction objects to the database.
        This method must be implemented by all subclasses.
        """
        raise NotImplementedError


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
