from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from images.models import AerialGeoreference, Comment, Georeference


class Command(BaseCommand):
    help = (
        "Show site activity (georeferences, comments, engaged users) "
        "during a time period"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--start",
            help="Start of the period, YYYY-MM-DD (inclusive)",
        )
        parser.add_argument(
            "--end",
            help="End of the period, YYYY-MM-DD (inclusive; defaults to today)",
        )
        parser.add_argument(
            "--days",
            type=int,
            help="Shortcut for --start: the last N days (ignored if --start is given)",
        )

    def handle(self, *args, **options):
        start, end = self.parse_period(options)

        point_qs = Georeference.objects.filter(
            georeferenced_at__gte=start, georeferenced_at__lt=end
        )
        aerial_qs = AerialGeoreference.objects.filter(
            georeferenced_at__gte=start, georeferenced_at__lt=end
        )
        comment_qs = Comment.objects.filter(created_at__gte=start, created_at__lt=end)

        point_count = point_qs.count()
        aerial_count = aerial_qs.count()
        comment_count = comment_qs.count()

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Site activity from {start:%Y-%m-%d %H:%M} "
                f"to {end:%Y-%m-%d %H:%M} ({timezone.get_current_timezone_name()})"
            )
        )
        self.stdout.write("")
        self.stdout.write(f"Georeferences: {point_count + aerial_count}")
        self.stdout.write(f"  Point:       {point_count}")
        self.stdout.write(f"  From above:  {aerial_count}")
        self.stdout.write(f"Comments:      {comment_count}")

        self.print_collection_breakdown(point_qs, aerial_qs, comment_qs)
        self.print_users(point_qs, aerial_qs, comment_qs)

    def parse_period(self, options):
        now = timezone.localtime()

        if options["start"]:
            start = self.aware_date(options["start"], "--start")
        elif options["days"]:
            start = now - timedelta(days=options["days"])
        else:
            raise CommandError("Specify a period with --start (and --end) or --days.")

        if options["end"]:
            # Inclusive end date: filter up to midnight of the following day
            end = self.aware_date(options["end"], "--end") + timedelta(days=1)
        else:
            end = now

        if end <= start:
            raise CommandError("End of period must be after its start.")
        return start, end

    def aware_date(self, value, flag):
        try:
            date = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(f"Invalid {flag} date {value!r}, expected YYYY-MM-DD.")
        return timezone.make_aware(datetime.combine(date, time.min))

    def print_collection_breakdown(self, point_qs, aerial_qs, comment_qs):
        breakdown = {}

        def add(qs, key):
            rows = qs.values(
                "image__collection_id",
                "image__collection__name",
                "image__collection__source__name",
            ).annotate(n=Count("pk"))
            for row in rows:
                entry = breakdown.setdefault(
                    row["image__collection_id"],
                    {
                        "label": (
                            f"{row['image__collection__source__name']} — "
                            f"{row['image__collection__name']}"
                        ),
                        "point": 0,
                        "aerial": 0,
                        "comments": 0,
                    },
                )
                entry[key] = row["n"]

        add(point_qs, "point")
        add(aerial_qs, "aerial")
        add(comment_qs, "comments")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("By collection"))
        if not breakdown:
            self.stdout.write("  (no activity)")
            return

        entries = sorted(
            breakdown.values(),
            key=lambda e: e["point"] + e["aerial"] + e["comments"],
            reverse=True,
        )
        label_width = max(len(e["label"]) for e in entries)
        header = f"  {'Collection':<{label_width}}  {'Point':>6} {'Above':>6} {'Comments':>9}"
        self.stdout.write(header)
        for e in entries:
            self.stdout.write(
                f"  {e['label']:<{label_width}}  "
                f"{e['point']:>6} {e['aerial']:>6} {e['comments']:>9}"
            )

    def print_users(self, point_qs, aerial_qs, comment_qs):
        def user_ids(qs, field):
            return set(
                qs.exclude(**{field: None})
                .values_list(f"{field}_id", flat=True)
                .distinct()
            )

        users = (
            user_ids(point_qs, "georeferenced_by")
            | user_ids(aerial_qs, "georeferenced_by")
            | user_ids(comment_qs, "commented_by")
        )
        anonymous = (
            point_qs.filter(georeferenced_by=None).count()
            + aerial_qs.filter(georeferenced_by=None).count()
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Engaged users"))
        self.stdout.write(f"  Logged-in users: {len(users)}")
        self.stdout.write(f"  Anonymous georeference submissions: {anonymous}")
