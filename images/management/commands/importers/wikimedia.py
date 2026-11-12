import re
from time import sleep
import requests
from tqdm import tqdm

from images.models import Collection, Image, Source
from images.tasks import generate_iiif_tiles
from images.utils import R2Uploader

POLITE_WAIT_SECS = 2.0  # Wikimedia is generally faster than LoC but still requires respect

headers = {"User-Agent": "Yesterdays/1.0 (https://maprva.org)"}

def add_arguments(parser):
    parser.add_argument("--max-items", type=int, default=100, help="Max items to process")
    parser.add_argument("--query", type=str, help="Initial search query hint")

def handle(options):
    source = get_or_create_wikimedia_source()
    collection_info = get_collection_info()
    collection = create_collection_if_not_exist(source, collection_info)

    if not collection:
        print("Collection creation cancelled.")
        return

    query = options["query"]
    r2_uploader = R2Uploader()
    handle_import(collection, query, options.get("license"), options['max_items'], r2_uploader)

def get_or_create_wikimedia_source():
    source, created = Source.objects.get_or_create(
        name="Wikimedia Commons",
        defaults={
            "url": "https://commons.wikimedia.org/",
            "slug": "wikimedia-commons",
            "description": "A media repository that is part of the Wikimedia Foundation.",
            "public": True,
        },
    )
    return source

def get_collection_info() -> dict:
    """Interactive prompt to get collection information from user"""
    print("\n=== Wikimedia Commons Collection Import ===")
    print("Please provide the following collection information:")

    collection_slug = input(
        "Collection URL slug (e.g., 'brueck-und-sohn'): "
    )

    # Check if a collection with this slug already exists in our database
    source = Source.objects.filter(name="Wikimedia Commons").first()
    if source:
        # Look for existing collection that would have the same final name
        existing_collections = Collection.objects.filter(
            source=source,
        )

        # Find collection that likely matches this slug
        matching_collection = None
        for collection in existing_collections:
            if collection.slug == collection_slug:
                matching_collection = collection
                break

        if matching_collection:
            print("\n⚠️  Found existing collection that may match this slug:")
            print(f"  Name: {matching_collection.name}")
            print(f"  URL: {matching_collection.url}")
            print(f"  Description: {matching_collection.description}")
            print(f"  Public: {'Yes' if matching_collection.public else 'No'}")
            print(f"  Images: {matching_collection.images.count()}")

            if input("\nUse this existing collection? [y/N] ").strip().lower() == "y":
                return {
                    "slug": collection_slug,
                    "name": matching_collection.name,
                    "description": matching_collection.description,
                    "existing_collection": matching_collection,
                }

def create_collection_if_not_exist(source, collection_info) -> Collection:
    """Get or create a collection based on collection info"""
    # Check if we already have an existing collection from the info gathering
    if "existing_collection" in collection_info:
        print(
            f"  ✓ Using existing collection: {collection_info['existing_collection'].name}"
        )
        return collection_info["existing_collection"]

    collection_name = collection_info["name"]
    collection_url = f"https://commons.wikimedia.org/wiki/Commons:{collection_info['name']}"

    # Check if collection already exists (shouldn't happen given our earlier check, but just in case)
    existing_collection = Collection.objects.filter(
        source=source, name=collection_name
    ).first()
    if existing_collection:
        print(f"  ✓ Using existing collection: {existing_collection.name}")
        return existing_collection

    # Show collection details to user for confirmation
    print("\n  Collection Details:")
    print(f"  Name: {collection_name}")
    print(f"  Source: {source.name}")
    print(f"  URL: {collection_url}")
    print(f"  Description: {collection_info['description']}")

    if input("\n  Create this collection? [y/N] ").strip().lower() == "y":
        collection = Collection.objects.create(
            source=source,
            name=collection_name,
            url=collection_url,
            description=collection_info["description"],
            public=False,
        )
        print(f"Created PRIVATE collection: {collection.name}")
        return collection
    else:
        return None

def fetch_wikimedia_page(query, category, continue_token=None):
    api_url = "https://commons.wikimedia.org/w/api.php"
    search_str = f'{query} incategory:"Images_from_{category}"'

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": search_str,
        "gsrnamespace": 6,
        "gsrlimit": 50,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|timestamp",
    }

    if continue_token:
        params.update(continue_token)

    response = requests.get(api_url, params=params, headers=headers)
    response.raise_for_status()
    return response.json()

def handle_import(collection, query, license_, max_items, r2_uploader):
    processed_count = 0
    page_num = 1
    continue_token = None

    while True:
        data = fetch_wikimedia_page(query, collection.name, continue_token)
        pages = data.get("query", {}).get("pages", {}).values()
        for page in tqdm(pages, desc=f"Page {page_num}"):
            if max_items and processed_count >= max_items:
                return

            img_info = page.get("imageinfo", [{}])[0]
            metadata = img_info.get("extmetadata", {})

            ref = str(page.get("pageid"))
            if Image.objects.filter(ref=ref).exists():
                continue

            file_url = img_info.get("url")
            title = page.get("title", "").replace("File:", "")
            if "Brück" in title:
                # Extracting title from Brueck und Sohn file name
                match = re.search(r'-\d{4}-(.+?)-Brück', title)
                if match:
                    title = match.group(1)
                else:
                    tqdm.write(f"      ⚠ Failed to extract title: {title}")

            # Metadata Extraction
            description = metadata.get("ImageDescription", {}).get("value", "")
            # Clean HTML tags often found in Wikimedia descriptions
            description = re.sub('<[^<]+?>', '', description)
            lat = metadata.get("GPSLatitude", {}).get("value", "")
            if lat:
                description += f"\nLatitude: {lat}"
            lon = metadata.get("GPSLongitude", {}).get("value", "")
            if lon:
                description += f"\nLongitude: {lon}"

            creator = metadata.get("Artist", {}).get("value", "")
            creator = re.sub('<[^<]+?>', '', creator)
            creator = creator.replace("&amp;", "&")

            original_date = metadata.get("DateTimeOriginal", {}).get("value", "")
            # Placeholder for the parse_loc_date style logic if needed
            edtf_date = original_date

            try:
                image = Image.objects.create(
                    collection=collection,
                    title=title[:255],
                    permalink=file_url,
                    ref=ref,
                    original_url=f"https://commons.wikimedia.org/entity/M{ref}",
                    description=description,
                    creator=creator,
                    original_date=original_date,
                    edtf_date=edtf_date,
                    license=license_,
                )
                tqdm.write(f"      → Created image ID: {image.id}")

                # R2 Upload & Tiling
                r2_url = r2_uploader.upload_original(image.id, file_url, in_tqdm=True)
                if r2_url:
                    Image.objects.filter(pk=image.id).update(permalink=r2_url)
                    generate_iiif_tiles.delay(image.id)
                else:
                    tqdm.write("      ⚠ Failed to upload original, keeping source URL")

                processed_count += 1
                sleep(POLITE_WAIT_SECS)

            except Exception as e:
                print(f"Error importing {title}: {e}")

        continue_token = data.get("continue")
        page_num += 1
        if not continue_token:
            break
