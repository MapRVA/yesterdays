from django.db.models import Q
from django_filters import rest_framework as filters
from rest_framework.exceptions import ValidationError

from images.models import (
    AerialGeoreference,
    Collection,
    Georeference,
    Image,
    Source,
)
from subjects.models import Subject


class NumberInFilter(filters.BaseInFilter, filters.NumberFilter):
    pass


class CharInFilter(filters.BaseInFilter, filters.CharFilter):
    pass


def _filter_georeferenced_by(queryset, name, value):
    raw_values = str(value).split(",")

    usernames = []
    for v in raw_values:
        try:
            osm_id = int(v.strip())
        except (ValueError, TypeError):
            raise ValidationError(
                {"georeferenced_by": "Must be integers (OSM user IDs) separated by commas."}
            )

        if osm_id == 0:
            usernames.append("hardcoded_admin")
        else:
            usernames.append(f"osm_{osm_id}")

    return queryset.filter(georeferenced_by__username__in=usernames)


def _parse_subject_values(value):
    """Parse mixed list/string into separate PK IDs and Wikidata Q-IDs."""
    if isinstance(value, str):
        raw_values = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, (list, tuple)):
        raw_values = value
    else:
        raw_values = [value]

    pk_ids = [int(v) for v in raw_values if str(v).strip().isdigit()]
    wikidata_ids = [str(v).strip() for v in raw_values if not str(v).strip().isdigit()]

    return pk_ids, wikidata_ids


def _filter_queryset_by_subject(queryset, prefix, value):
    """Shared subject filtering logic across FilterSet classes."""
    pk_ids, wikidata_ids = _parse_subject_values(value)
    subject_query = Q()

    if pk_ids:
        subject_query |= Q(**{f"{prefix}subject_mappings__subject_id__in": pk_ids})
    if wikidata_ids:
        subject_query |= Q(
            **{f"{prefix}subject_mappings__subject__wikidata_item__wikidata_id__in": wikidata_ids}
        )

    if not subject_query:
        return queryset.none()

    return queryset.filter(subject_query).distinct()


class SourceFilter(filters.FilterSet):
    slug = CharInFilter(field_name="slug", lookup_expr="in")

    class Meta:
        model = Source
        fields = ["slug"]


class CollectionFilter(filters.FilterSet):
    source = NumberInFilter(field_name="source_id", lookup_expr="in")
    slug = CharInFilter(field_name="slug", lookup_expr="in")

    class Meta:
        model = Collection
        fields = ["source", "slug"]


class SubjectFilter(filters.FilterSet):
    slug = CharInFilter(field_name="slug", lookup_expr="in")

    class Meta:
        model = Subject
        fields = ["slug"]


class ImageFilter(filters.FilterSet):
    source = NumberInFilter(field_name="collection__source_id", lookup_expr="in")
    collection = NumberInFilter(field_name="collection_id", lookup_expr="in")
    subject = filters.CharFilter(method="filter_by_subject")
    creator = filters.CharFilter(lookup_expr="icontains")

    # Temporal filters: year-based ranges against the decimal date fields
    year_min = filters.NumberFilter(
        field_name="fuzzy_end_decdate",
        lookup_expr="gte",
        help_text="Minimum year (includes images that may extend into this year)",
    )
    year_max = filters.NumberFilter(
        field_name="fuzzy_start_decdate",
        lookup_expr="lte",
        help_text="Maximum year (includes images that may start before this year)",
    )

    # Georeferencing status
    georeferenced = filters.BooleanFilter(method="filter_georeferenced")

    # From-above images (aerial/bird's-eye)
    from_above = filters.BooleanFilter(field_name="aerial")

    class Meta:
        model = Image
        fields = []

    def filter_by_subject(self, queryset, name, value):
        return _filter_queryset_by_subject(queryset, "", value)

    def filter_georeferenced(self, queryset, name, value):
        has_point = Q(aerial=False, georeferences__isnull=False)
        has_aerial = Q(aerial=True, aerial_georeferences__isnull=False)
        if value:
            return queryset.filter(has_point | has_aerial).distinct()
        return queryset.exclude(has_point | has_aerial)


class GeoreferenceFilter(filters.FilterSet):
    image = NumberInFilter(field_name="image_id", lookup_expr="in")
    source = NumberInFilter(field_name="image__collection__source_id", lookup_expr="in")
    collection = NumberInFilter(field_name="image__collection_id", lookup_expr="in")
    subject = filters.CharFilter(method="filter_by_subject")
    confidence = filters.ChoiceFilter(
        choices=Georeference.CONFIDENCE_CHOICES,
    )
    from_above = filters.BooleanFilter(field_name="image__aerial")
    georeferenced_by = filters.CharFilter(
        method=_filter_georeferenced_by,
        help_text="Comma-separated OSM user IDs.",
    )
    year_min = filters.NumberFilter(
        field_name="image__fuzzy_end_decdate",
        lookup_expr="gte",
    )
    year_max = filters.NumberFilter(
        field_name="image__fuzzy_start_decdate",
        lookup_expr="lte",
    )

    class Meta:
        model = Georeference
        fields = []

    def filter_by_subject(self, queryset, name, value):
        return _filter_queryset_by_subject(queryset, "image__", value)


class FromAboveGeoreferenceFilter(filters.FilterSet):
    image = NumberInFilter(field_name="image_id", lookup_expr="in")
    source = NumberInFilter(field_name="image__collection__source_id", lookup_expr="in")
    collection = NumberInFilter(field_name="image__collection_id", lookup_expr="in")
    subject = filters.CharFilter(method="filter_by_subject")
    confidence = filters.ChoiceFilter(
        choices=AerialGeoreference.CONFIDENCE_CHOICES,
    )
    georeferenced_by = filters.CharFilter(
        method=_filter_georeferenced_by,
        help_text="Comma-separated OSM user IDs.",
    )
    year_min = filters.NumberFilter(
        field_name="image__fuzzy_end_decdate",
        lookup_expr="gte",
    )
    year_max = filters.NumberFilter(
        field_name="image__fuzzy_start_decdate",
        lookup_expr="lte",
    )

    class Meta:
        model = AerialGeoreference
        fields = []

    def filter_by_subject(self, queryset, name, value):
        return _filter_queryset_by_subject(queryset, "image__", value)
