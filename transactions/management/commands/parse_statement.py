import os
from django.core.management.base import BaseCommand, CommandError
from transactions.parsers import StatementParser, get_parser_choices

class Command(BaseCommand):
    help = 'Parses a PDF bank statement and saves the transactions to the database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--parser',
            type=str,
            required=True,
            choices=get_parser_choices(),
            help='The name of the parser to use.'
        )
        parser.add_argument('pdf_files', nargs='+', type=str, help='The path to the PDF file(s) to parse.')

    def handle(self, *args, **options):
        parser_name = options['parser']
        
        # Find the correct parser class from the registry
        parser_class = next((
            subclass for subclass in StatementParser.__subclasses__()
            if getattr(subclass, 'parser_name', None) == parser_name
        ), None)

        if not parser_class:
            raise CommandError(f"Parser '{parser_name}' not found.")

        for pdf_path in options['pdf_files']:
            if not os.path.exists(pdf_path):
                raise CommandError(f"File not found at: {pdf_path}")

            self.stdout.write(self.style.NOTICE(f"Starting parsing for '{pdf_path}' using '{parser_name}' parser."))
            try:
                parser_instance = parser_class(statement_path=pdf_path)
                parser_instance.parse()
                self.stdout.write(self.style.SUCCESS(f'Successfully parsed and saved transactions from: {pdf_path}'))
            except Exception as e:
                raise CommandError(f"An error occurred while parsing {pdf_path}: {e}")
