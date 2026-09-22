"""Wholesale-rebuild the project-subject markers from current Subject rows."""

from django.core.management.base import BaseCommand

from subjects.project_graph import rebuild_project_graph


class Command(BaseCommand):
    help = (
        "Rebuild the :ProjectSubject markers in Memgraph from the current "
        "set of Subject rows that have a linked WikidataItem."
    )

    def handle(self, *args, **options):
        count = rebuild_project_graph()
        self.stdout.write(
            self.style.SUCCESS(f"Project graph rebuilt with {count} subject marker(s)")
        )
