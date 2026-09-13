from .album import (
    add_image_to_album,
    album_detail,
    bulk_add_to_album,
    bulk_create_and_add_to_album,
    create_and_add_to_album,
    delete_album,
    edit_album,
    remove_image_from_album,
    toggle_album_public,
    user_albums_api,
)
from .api import (
    aerial_geojson_endpoint,
    geojson_endpoint,
    get_tile_version,
    osm_elements_vector_tiles_endpoint,
    polygonal_georeferences_at_point,
    vector_tiles_endpoint,
)
from .browse import (
    browse_aerials,
    browse_sources,
    collection_detail,
    image_detail,
    image_list,
    source_detail,
    top_rated_images,
)
from .core import (
    add_comment,
    bulk_mark_aerial,
    bulk_mark_will_not_georef,
    get_min_scale_for_zoom,
    get_random_image,
    image_stats,
    label_scales,
    map_embed,
    mark_aerial,
    mark_difficulty,
    mark_scale,
    mark_will_not_georef,
    skip_image,
    submit_rating,
    update_image_scale,
)
from .duplicates import (
    dismiss_duplicate_pair,
    duplicate_image_pairs,
    duplicate_pair_detail,
    resolve_duplicate_pair,
    restore_duplicate_pair,
)
from .featured_image import (
    featured_image_delete,
    featured_image_edit,
    featured_image_queue,
    queue_featured_image,
    user_autocomplete,
)
from .georeference import (
    aerial_georeference_image,
    aerial_georeference_interface,
    georeference_image,
    georeference_interface,
    validate_georeference,
)
from .search import (
    find_similar_images,
    in_view_search,
    reverse_image_search,
    search_page,
    semantic_search,
    text_search,
)
from .validation_queue import validation_queue
