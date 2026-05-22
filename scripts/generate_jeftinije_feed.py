import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-04")
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ADMIN_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN")

PUBLIC_STORE_DOMAIN = os.getenv("PUBLIC_STORE_DOMAIN", "www.luvmechanics.com")

OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "docs/jeftinije.xml"))

# Empty = default Shopify language.
# FEED_LOCALE=""   -> English/default feed
# FEED_LOCALE="hr" -> Croatian translated feed
FEED_LOCALE = os.getenv("FEED_LOCALE", "").strip()

MIN_QUANTITY = int(os.getenv("MIN_QUANTITY", "3"))
DEFAULT_CATEGORY = os.getenv(
    "DEFAULT_CATEGORY",
    "Erotska pomagala > Ostala erotska pomagala i pribor",
)
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")

# Jeftinije.hr supported values:
# in stock / preorder / out of stock
DEFAULT_STOCK = os.getenv("DEFAULT_STOCK", "preorder")

DELIVERY_COST = os.getenv("DELIVERY_COST", "6.90")
DELIVERY_TIME_MIN = os.getenv("DELIVERY_TIME_MIN", "4")
DELIVERY_TIME_MAX = os.getenv("DELIVERY_TIME_MAX", "8")

# If true, products without a valid EAN-13 are excluded.
REQUIRE_EAN = os.getenv("REQUIRE_EAN", "false").lower() == "true"


GRAPHQL_QUERY = """
query ProductVariants($cursor: String) {
  productVariants(first: 100, after: $cursor, query: "product_status:active") {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      title
      sku
      barcode
      price
      compareAtPrice
      inventoryQuantity
      image {
        url
      }
      selectedOptions {
        name
        value
      }
      product {
        id
        title
        handle
        vendor
        productType
        tags
        targetGender: metafield(namespace: "custom", key: "target_gender") {
          value
        }
        description
        descriptionHtml
        status
        onlineStoreUrl
        collections(first: 20) {
          nodes {
            title
            handle
          }
        }
        images(first: 10) {
          nodes {
            url
          }
        }
        featuredMedia {
          preview {
            image {
              url
            }
          }
        }
      }
    }
  }
}
"""


TRANSLATABLE_RESOURCE_QUERY = """
query TranslatableResource($resourceId: ID!, $locale: String!) {
  translatableResource(resourceId: $resourceId) {
    resourceId
    translations(locale: $locale) {
      key
      value
      outdated
    }
  }
}
"""


def require_env() -> None:
    missing = []

    if not SHOPIFY_STORE:
        missing.append("SHOPIFY_STORE")

    if not SHOPIFY_ADMIN_TOKEN:
        missing.append("SHOPIFY_ADMIN_TOKEN")

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def shopify_graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

    payload = json.dumps(
        {
            "query": query,
            "variables": variables or {},
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": SHOPIFY_ADMIN_TOKEN,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw_response = response.read().decode("utf-8")
            result = json.loads(raw_response)

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Shopify API HTTP error {error.code}: {body}") from error

    except urllib.error.URLError as error:
        raise RuntimeError(f"Shopify API connection error: {error}") from error

    if "errors" in result:
        raise RuntimeError(
            f"Shopify GraphQL errors: {json.dumps(result['errors'], ensure_ascii=False)}"
        )

    return result["data"]


def fetch_variants() -> List[Dict[str, Any]]:
    variants: List[Dict[str, Any]] = []
    cursor = None

    while True:
        data = shopify_graphql(GRAPHQL_QUERY, {"cursor": cursor})
        connection = data["productVariants"]

        variants.extend(connection["nodes"])

        page_info = connection["pageInfo"]

        if not page_info["hasNextPage"]:
            break

        cursor = page_info["endCursor"]
        time.sleep(0.25)

    return variants


def fetch_translations(resource_id: str, locale: str) -> Dict[str, str]:
    if not locale:
        return {}

    try:
        data = shopify_graphql(
            TRANSLATABLE_RESOURCE_QUERY,
            {
                "resourceId": resource_id,
                "locale": locale,
            },
        )
    except RuntimeError as error:
        error_text = str(error)

        # Some resources may not be translatable or may return RESOURCE_NOT_FOUND.
        # We skip them and fall back to default Shopify values.
        if "RESOURCE_NOT_FOUND" in error_text or "Invalid id" in error_text:
            print(f"Skipping translations for missing resource: {resource_id}")
            return {}

        raise

    resource = data.get("translatableResource") or {}
    translations = resource.get("translations") or []

    translated_values: Dict[str, str] = {}

    for translation in translations:
        key = translation.get("key")
        value = translation.get("value")

        if key and value:
            translated_values[key] = value

    return translated_values


def attach_translations(variants: List[Dict[str, Any]], locale: str) -> None:
    if not locale:
        print("FEED_LOCALE is empty; using default Shopify language.")
        return

    product_ids = sorted(
        {
            variant["product"]["id"]
            for variant in variants
            if variant.get("product") and variant["product"].get("id")
        }
    )

    print(f"Fetching translations for locale: {locale}")
    print(f"Products to translate: {len(product_ids)}")

    product_translations: Dict[str, Dict[str, str]] = {}

    for index, product_id in enumerate(product_ids, start=1):
        product_translations[product_id] = fetch_translations(product_id, locale)

        if index % 25 == 0:
            print(f"Fetched product translations: {index}/{len(product_ids)}")

        time.sleep(0.15)

    for variant in variants:
        product = variant.get("product") or {}

        product["_translations"] = product_translations.get(product.get("id"), {})
        variant["_translations"] = {}


def numeric_id(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


def collapse_whitespace(value: Optional[str]) -> str:
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"\s+", " ", value).strip()

    return value


def clean_html_description(value: Optional[str], fallback_text: Optional[str] = None) -> str:
    """
    Converts Shopify HTML description into clean plain text.
    Text is still wrapped in CDATA later, but HTML tags are removed.
    """
    if not value:
        value = fallback_text or ""

    description = str(value)

    # Remove risky/unnecessary blocks.
    description = re.sub(
        r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>",
        " ",
        description,
        flags=re.I,
    )
    description = re.sub(
        r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>",
        " ",
        description,
        flags=re.I,
    )

    # Remove HTML comments.
    description = re.sub(r"<!--.*?-->", " ", description, flags=re.S)

    # Convert common block/list tags into readable punctuation before stripping tags.
    description = re.sub(r"</p\s*>", ". ", description, flags=re.I)
    description = re.sub(r"<br\s*/?>", ". ", description, flags=re.I)
    description = re.sub(r"</li\s*>", ". ", description, flags=re.I)
    description = re.sub(r"<li\b[^>]*>", " ", description, flags=re.I)
    description = re.sub(r"</h[1-6]\s*>", ". ", description, flags=re.I)
    description = re.sub(r"</div\s*>", ". ", description, flags=re.I)

    # Remove all remaining HTML tags.
    description = re.sub(r"<[^>]+>", " ", description)

    # Decode HTML entities.
    description = html.unescape(description)

    # Clean leftover spacing and punctuation.
    description = re.sub(r"\s+", " ", description)
    description = re.sub(r"\s+\.", ".", description)
    description = re.sub(r"\.{2,}", ".", description)
    description = re.sub(r"\s+,", ",", description)
    description = description.strip(" .")

    return description


def resource_translation(resource: Dict[str, Any], key: str) -> str:
    translations = resource.get("_translations") or {}
    return translations.get(key) or ""


def translated_product_title(product: Dict[str, Any]) -> str:
    return resource_translation(product, "title") or collapse_whitespace(product.get("title"))


def translated_product_description_html(product: Dict[str, Any]) -> str:
    return resource_translation(product, "body_html") or product.get("descriptionHtml") or ""


def translated_product_description_text(product: Dict[str, Any]) -> str:
    return resource_translation(product, "body") or product.get("description") or ""


def translated_product_handle(product: Dict[str, Any]) -> str:
    return resource_translation(product, "handle") or collapse_whitespace(product.get("handle"))


def translated_variant_title(variant: Dict[str, Any]) -> str:
    return resource_translation(variant, "title") or collapse_whitespace(variant.get("title"))


def limit_text(value: str, max_length: int) -> str:
    value = value.strip()

    if len(value) <= max_length:
        return value

    return value[:max_length].rstrip()


def clean_brand(value: Any) -> str:
    brand = collapse_whitespace(value)

    if not brand:
        return ""

    return brand.upper()


def remove_brand_from_title(title: str, brand: str) -> str:
    title = collapse_whitespace(title)
    brand = collapse_whitespace(brand)

    if not title or not brand:
        return title

    # Remove brand only if it appears at the beginning of the title.
    # Example: "KIIROO SPOT KISS ME VIBRATOR" -> "SPOT KISS ME VIBRATOR"
    pattern = rf"^{re.escape(brand)}[\s\-:|]+"
    title = re.sub(pattern, "", title, flags=re.I).strip()

    return title


def capitalize_title_text(value: str) -> str:
    value = collapse_whitespace(value)

    if not value:
        return ""

    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value).strip()

    words = []

    for word in value.split(" "):
        if not word:
            continue

        upper_word = word.upper()

        # Keep technical words readable.
        if upper_word in {
            "USB",
            "USB-C",
            "G",
            "G-TOČKA",
            "G-TOCKA",
            "APP",
            "WIFI",
            "WI-FI",
            "IPX7",
            "ABS",
            "TPE",
            "BPA",
        }:
            words.append(upper_word)
            continue

        # Preserve words with digits mostly uppercase/readable: SONA 3, TOR 2, IPX7.
        if any(char.isdigit() for char in word):
            words.append(upper_word)
            continue

        if "-" in word:
            parts = [
                part[:1].upper() + part[1:].lower()
                for part in word.split("-")
                if part
            ]
            words.append("-".join(parts))
            continue

        words.append(word[:1].upper() + word[1:].lower())

    return " ".join(words)


def feed_product_title(product: Dict[str, Any]) -> str:
    brand = clean_brand(product.get("vendor"))
    raw_title = translated_product_title(product)

    title_without_brand = remove_brand_from_title(raw_title, brand)
    title = capitalize_title_text(title_without_brand)

    if brand and title:
        return f"{brand} {title}"

    if brand:
        return brand

    return title


def normalize_price(value: Any) -> str:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return ""

    if price <= 0:
        return ""

    return f"{price:.2f}"


def is_valid_ean13(digits: str) -> bool:
    if not re.fullmatch(r"\d{13}", digits):
        return False

    # Exclude restricted GS1 ranges: 02, 04, 2 and coupon ranges 98-99.
    if digits.startswith(("02", "04", "2", "98", "99")):
        return False

    check_sum = 0

    for index, char in enumerate(digits[:12]):
        digit = int(char)
        check_sum += digit if index % 2 == 0 else digit * 3

    expected_check_digit = (10 - (check_sum % 10)) % 10

    return expected_check_digit == int(digits[-1])


def normalize_ean(value: Optional[str]) -> str:
    if not value:
        return ""

    digits = re.sub(r"\D", "", value)

    if is_valid_ean13(digits):
        return digits

    return ""


def product_url(product: Dict[str, Any], variant_id: str) -> str:
    translated_handle = translated_product_handle(product)

    if FEED_LOCALE and translated_handle:
        return f"https://{PUBLIC_STORE_DOMAIN}/{FEED_LOCALE}/products/{translated_handle}?variant={variant_id}"

    online_url = product.get("onlineStoreUrl")

    if online_url:
        separator = "&" if "?" in online_url else "?"
        return f"{online_url}{separator}variant={variant_id}"

    handle = translated_handle or product.get("handle")

    if handle:
        return f"https://{PUBLIC_STORE_DOMAIN}/products/{handle}?variant={variant_id}"

    return ""


def all_image_urls(variant: Dict[str, Any]) -> List[str]:
    urls: List[str] = []

    variant_image = variant.get("image") or {}

    if variant_image.get("url"):
        urls.append(variant_image["url"])

    product = variant.get("product") or {}
    images = ((product.get("images") or {}).get("nodes")) or []

    for image in images:
        url = image.get("url")

        if url and url not in urls:
            urls.append(url)

    media = product.get("featuredMedia") or {}
    preview = media.get("preview") or {}
    image = preview.get("image") or {}
    featured_url = image.get("url")

    if featured_url and featured_url not in urls:
        urls.append(featured_url)

    return urls


def main_image_url(variant: Dict[str, Any]) -> str:
    urls = all_image_urls(variant)
    return urls[0] if urls else ""


def more_image_urls(variant: Dict[str, Any], max_length: int = 1000) -> str:
    """
    Adds full image URLs one by one until the 1000-character limit is reached.
    This prevents cutting a URL in the middle.
    """
    urls = all_image_urls(variant)

    if len(urls) <= 1:
        return ""

    selected_urls: List[str] = []
    current_length = 0

    for url in urls[1:]:
        url = url.strip()

        if not url:
            continue

        additional_length = len(url) if not selected_urls else len(url) + 1

        if current_length + additional_length > max_length:
            break

        selected_urls.append(url)
        current_length += additional_length

    return ",".join(selected_urls)


def normalize_category_key(value: Any) -> str:
    if value is None:
        return ""

    text = html.unescape(str(value)).lower()
    text = text.replace("&", "and")
    text = text.replace("+", "plus")
    text = text.replace("'", "")
    text = text.replace("’", "")
    text = text.replace("đ", "d")

    text = re.sub(r"[^a-z0-9čćšž]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")

    return text


IGNORED_COLLECTION_KEYS = {
    "all",
    "all-products",
    "bestsellers",
    "best-sellers",
    "new-arrivals",
    "new",
    "featured",
    "frontpage",
    "homepage",
    "home-page",
    "for-men",
    "men",
    "for-women",
    "women",
    "couples",
    "for-couples",
    "sale",
    "sales",
    "discount",
    "discounts",
    "deals",
    "bundles",
    "bundle",
    "premium",
    "beginner",
    "confident-pleasure",
    "curious-explorers",
}


SHOPIFY_COLLECTION_TO_JEFTINIJE_CATEGORY = {
    # Vibratori
    "vibrators": "Erotska pomagala > Vibratori",
    "vibrator": "Erotska pomagala > Vibratori",
    "vibratori": "Erotska pomagala > Vibratori",
    "rabbit-vibrators": "Erotska pomagala > Vibratori",
    "rabbit-vibrator": "Erotska pomagala > Vibratori",
    "rabbit-vibratori": "Erotska pomagala > Vibratori",
    "clitoris-stimulators": "Erotska pomagala > Vibratori",
    "clitoral-stimulators": "Erotska pomagala > Vibratori",
    "clitoris-stimulator": "Erotska pomagala > Vibratori",
    "clitoral-stimulator": "Erotska pomagala > Vibratori",
    "stimulatori-klitorisa": "Erotska pomagala > Vibratori",
    "stimulator-klitorisa": "Erotska pomagala > Vibratori",
    "wand-vibrators": "Erotska pomagala > Vibratori",
    "wand-massagers": "Erotska pomagala > Vibratori",
    "massagers": "Erotska pomagala > Vibratori",
    "masazeri": "Erotska pomagala > Vibratori",
    "masažeri": "Erotska pomagala > Vibratori",

    # Masturbatori
    "masturbators": "Erotska pomagala > Masturbatori",
    "masturbator": "Erotska pomagala > Masturbatori",
    "masturbatori": "Erotska pomagala > Masturbatori",
    "male-masturbators": "Erotska pomagala > Masturbatori",
    "strokers": "Erotska pomagala > Masturbatori",
    "stroker": "Erotska pomagala > Masturbatori",
    "automatic-masturbators": "Erotska pomagala > Masturbatori",
    "interactive-masturbators": "Erotska pomagala > Masturbatori",

    # Erekcijske pumpe
    "penis-pumps": "Erotska pomagala > Erekcijske pumpe",
    "penis-pump": "Erotska pomagala > Erekcijske pumpe",
    "erekcijske-pumpe": "Erotska pomagala > Erekcijske pumpe",
    "pumpe-za-penis": "Erotska pomagala > Erekcijske pumpe",

    # Povećavanje penisa
    "penis-enlargement": "Erotska pomagala > Povećavanje penisa",
    "penis-enlargers": "Erotska pomagala > Povećavanje penisa",
    "povecavanje-penisa": "Erotska pomagala > Povećavanje penisa",
    "povećavanje-penisa": "Erotska pomagala > Povećavanje penisa",

    # Erekcijski prsteni
    "penis-rings": "Erotska pomagala > Erekcijski prsteni",
    "cock-rings": "Erotska pomagala > Erekcijski prsteni",
    "erection-rings": "Erotska pomagala > Erekcijski prsteni",
    "erekcijski-prsteni": "Erotska pomagala > Erekcijski prsteni",
    "prstenovi-za-penis": "Erotska pomagala > Erekcijski prsteni",

    # Analni pribor
    "anal-toys": "Erotska pomagala > Analni pribor",
    "anal": "Erotska pomagala > Analni pribor",
    "analni-pribor": "Erotska pomagala > Analni pribor",
    "butt-plugs": "Erotska pomagala > Analni pribor",
    "prostate-massagers": "Erotska pomagala > Analni pribor",

    # Dildo
    "dildos": "Erotska pomagala > Umjetni penis (dildo)",
    "dildo": "Erotska pomagala > Umjetni penis (dildo)",
    "umjetni-penis": "Erotska pomagala > Umjetni penis (dildo)",

    # Vaginalne kuglice
    "vaginal-balls": "Erotska pomagala > Vaginalne kuglice",
    "kegel-balls": "Erotska pomagala > Vaginalne kuglice",
    "vaginalne-kuglice": "Erotska pomagala > Vaginalne kuglice",

    # Kompleti
    "couples-sets": "Erotska pomagala > Kompleti erotskog pribora",
    "couple-sets": "Erotska pomagala > Kompleti erotskog pribora",
    "sets-for-couples": "Erotska pomagala > Kompleti erotskog pribora",
    "kompleti": "Erotska pomagala > Kompleti erotskog pribora",
    "kompleti-erotskog-pribora": "Erotska pomagala > Kompleti erotskog pribora",

    # Fetiš / S&M
    "fetish": "Erotska pomagala > Fetiš (S & M) erotska pomagala",
    "fetish-sm": "Erotska pomagala > Fetiš (S & M) erotska pomagala",
    "bdsm": "Erotska pomagala > Fetiš (S & M) erotska pomagala",
    "s-m": "Erotska pomagala > Fetiš (S & M) erotska pomagala",
    "s-and-m": "Erotska pomagala > Fetiš (S & M) erotska pomagala",

    # Sex lutke
    "sex-dolls": "Erotska pomagala > Sex lutke",
    "sex-doll": "Erotska pomagala > Sex lutke",
    "sex-lutke": "Erotska pomagala > Sex lutke",

    # Ostalo / pribor
    "lubricants": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "lubricant": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "lubrikanti": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "cleaners": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "toy-cleaners": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "accessories": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "pribor": "Erotska pomagala > Ostala erotska pomagala i pribor",
    "other": "Erotska pomagala > Ostala erotska pomagala i pribor",
}


CATEGORY_PRIORITY = [
    "Erotska pomagala > Erekcijske pumpe",
    "Erotska pomagala > Povećavanje penisa",
    "Erotska pomagala > Erekcijski prsteni",
    "Erotska pomagala > Masturbatori",
    "Erotska pomagala > Analni pribor",
    "Erotska pomagala > Umjetni penis (dildo)",
    "Erotska pomagala > Vaginalne kuglice",
    "Erotska pomagala > Kompleti erotskog pribora",
    "Erotska pomagala > Fetiš (S & M) erotska pomagala",
    "Erotska pomagala > Sex lutke",
    "Erotska pomagala > Vibratori",
    "Erotska pomagala > Ostala erotska pomagala i pribor",
]


def product_collection_keys(product: Dict[str, Any]) -> List[str]:
    collections = ((product.get("collections") or {}).get("nodes")) or []
    keys: List[str] = []

    for collection in collections:
        title_key = normalize_category_key(collection.get("title"))
        handle_key = normalize_category_key(collection.get("handle"))

        for key in (handle_key, title_key):
            if key and key not in IGNORED_COLLECTION_KEYS and key not in keys:
                keys.append(key)

    return keys


def product_feed_category(variant: Dict[str, Any]) -> str:
    product = variant.get("product") or {}
    matched_categories: List[str] = []

    # 1. Prefer Shopify collections.
    for key in product_collection_keys(product):
        category = SHOPIFY_COLLECTION_TO_JEFTINIJE_CATEGORY.get(key)

        if category and category not in matched_categories:
            matched_categories.append(category)

    # 2. Fallback to Shopify productType.
    product_type_key = normalize_category_key(product.get("productType"))
    category = SHOPIFY_COLLECTION_TO_JEFTINIJE_CATEGORY.get(product_type_key)

    if category and category not in matched_categories:
        matched_categories.append(category)

    # 3. Fallback to product tags.
    for tag in product.get("tags") or []:
        tag_key = normalize_category_key(tag)
        category = SHOPIFY_COLLECTION_TO_JEFTINIJE_CATEGORY.get(tag_key)

        if category and category not in matched_categories:
            matched_categories.append(category)

    # 4. Choose by priority if multiple collections matched.
    for priority_category in CATEGORY_PRIORITY:
        if priority_category in matched_categories:
            return priority_category

    # 5. Final fallback.
    return DEFAULT_CATEGORY


ENGLISH_TO_CROATIAN_COLORS = {
    "black": "crna",
    "white": "bijela",
    "red": "crvena",
    "blue": "plava",
    "purple": "ljubičasta",
    "pink": "ružičasta",
    "gray": "siva",
    "grey": "siva",
    "green": "zelena",
    "orange": "narančasta",
    "yellow": "žuta",
    "teal": "tirkizna",
    "turquoise": "tirkizna",
    "fuchsia": "fuksija",
    "lilac": "lila",
    "indigo": "indigo",
    "brown": "smeđa",
    "transparent": "prozirna",
}


def translate_color_value(value: str) -> str:
    normalized = collapse_whitespace(value)
    key = normalized.lower()

    if FEED_LOCALE == "hr":
        return ENGLISH_TO_CROATIAN_COLORS.get(key, normalized)

    return normalized


def selected_options_map(variant: Dict[str, Any]) -> Dict[str, str]:
    options = {}

    for option in variant.get("selectedOptions") or []:
        name = collapse_whitespace(option.get("name")).lower()
        value = collapse_whitespace(option.get("value"))

        if name and value:
            options[name] = translate_color_value(value) if name in ("color", "colour", "boja") else value

    return options


def variant_color(variant: Dict[str, Any]) -> str:
    options = selected_options_map(variant)

    for key in ("color", "colour", "boja"):
        if key in options:
            return limit_text(options[key], 40)

    title = collapse_whitespace(translated_variant_title(variant))
    lower_title = title.lower()

    for english, croatian in ENGLISH_TO_CROATIAN_COLORS.items():
        if english in lower_title:
            return croatian if FEED_LOCALE == "hr" else english

    return ""


def product_target_gender(variant: Dict[str, Any]) -> str:
    product = variant.get("product") or {}
    metafield = product.get("targetGender") or {}

    value = collapse_whitespace(metafield.get("value"))

    if not value:
        return ""

    normalized = value.lower()

    if FEED_LOCALE == "hr":
        gender_map = {
            "female": "žene",
            "women": "žene",
            "woman": "žene",
            "male": "muškarci",
            "men": "muškarci",
            "man": "muškarci",
            "couples": "parovi",
            "couple": "parovi",
            "unisex": "unisex",
        }
    else:
        gender_map = {
            "female": "female",
            "women": "female",
            "woman": "female",
            "male": "male",
            "men": "male",
            "man": "male",
            "couples": "couples",
            "couple": "couples",
            "unisex": "unisex",
        }

    return gender_map.get(normalized, value)


def variant_size(variant: Dict[str, Any]) -> str:
    options = selected_options_map(variant)

    for key in ("size", "veličina", "velicina"):
        if key in options:
            return limit_text(options[key], 40)

    return ""


def variant_name(variant: Dict[str, Any]) -> str:
    product = variant.get("product") or {}

    product_title = feed_product_title(product)
    variant_title = collapse_whitespace(translated_variant_title(variant))

    if FEED_LOCALE == "hr":
        variant_title = translate_color_value(variant_title)
    else:
        variant_title = capitalize_title_text(variant_title)

    if variant_title and variant_title.lower() != "default title":
        return limit_text(f"{product_title}, {variant_title}", 200)

    return limit_text(product_title, 200)


def has_variant_title(variant: Dict[str, Any]) -> bool:
    title = collapse_whitespace(translated_variant_title(variant))
    return bool(title and title.lower() != "default title")


def should_include_variant(variant: Dict[str, Any]) -> bool:
    product = variant.get("product") or {}

    if product.get("status") != "ACTIVE":
        return False

    quantity = variant.get("inventoryQuantity")

    if quantity is None or int(quantity) < MIN_QUANTITY:
        return False

    if not normalize_price(variant.get("price")):
        return False

    if not collapse_whitespace(variant.get("sku")):
        return False

    if not main_image_url(variant):
        return False

    if not product_url(product, numeric_id(variant["id"])):
        return False

    if REQUIRE_EAN and not normalize_ean(variant.get("barcode")):
        return False

    return True


def cdata(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("]]>", "]]]]><![CDATA[>")
    return f"<![CDATA[{text}]]>"


def xml_escape(value: Any) -> str:
    text = "" if value is None else str(value)

    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def text_tag(tag: str, value: Any, indent: int = 1, use_cdata: bool = True) -> str:
    prefix = "\t" * indent
    content = cdata(value) if use_cdata else xml_escape(value)

    return f"{prefix}<{tag}>{content}</{tag}>"


def numeric_tag(tag: str, value: Any, indent: int = 1) -> str:
    return text_tag(tag, value, indent=indent, use_cdata=False)


def build_attributes_xml(variant: Dict[str, Any]) -> List[str]:
    product = variant.get("product") or {}

    brand = limit_text(collapse_whitespace(product.get("vendor")), 50)
    sku = limit_text(collapse_whitespace(variant.get("sku")), 20)
    color = variant_color(variant)
    size = variant_size(variant)
    category = product_feed_category(variant)
    target_gender = product_target_gender(variant)

    lines = []
    lines.append("\t<attributes>")
    lines.append(text_tag("gender", target_gender, indent=2, use_cdata=True))
    lines.append(text_tag("color", color, indent=2, use_cdata=True))
    lines.append(text_tag("size", size, indent=2, use_cdata=True))
    lines.append(text_tag("ageGroup", "adult", indent=2, use_cdata=False))

    attribute_values = {
        "Brand": brand,
        "SKU": sku,
        "Kategorija": category,
    }

    if target_gender:
        attribute_values["Namjena"] = target_gender

    if color:
        attribute_values["Boja"] = color

    if size:
        attribute_values["Veličina"] = size

    for name, value in attribute_values.items():
        if not value:
            continue

        lines.append("\t\t<attribute>")
        lines.append(text_tag("name", limit_text(name, 100), indent=3, use_cdata=True))
        lines.append("\t\t\t<values>")
        lines.append(text_tag("value", limit_text(value, 100), indent=4, use_cdata=True))
        lines.append("\t\t\t</values>")
        lines.append("\t\t</attribute>")

    lines.append("\t</attributes>")

    return lines


def build_item_xml(variant: Dict[str, Any]) -> str:
    product = variant["product"]
    variant_id = numeric_id(variant["id"])
    product_id = numeric_id(product["id"])

    price = normalize_price(variant.get("price"))
    compare_at_price = normalize_price(variant.get("compareAtPrice"))

    ean = normalize_ean(variant.get("barcode"))
    sku = limit_text(collapse_whitespace(variant.get("sku")), 20)
    brand = limit_text(collapse_whitespace(product.get("vendor")), 50)
    category = product_feed_category(variant)

    description = clean_html_description(
        translated_product_description_html(product),
        fallback_text=translated_product_description_text(product),
    )

    group_id = product_id if has_variant_title(variant) else ""

    lines = ["<Item>"]

    lines.append(text_tag("ID", variant_id))
    lines.append(text_tag("name", variant_name(variant)))
    lines.append(text_tag("description", description))
    lines.append(text_tag("link", limit_text(product_url(product, variant_id), 400)))
    lines.append(text_tag("mainImage", limit_text(main_image_url(variant), 200)))

    more_images = more_image_urls(variant)

    if more_images:
        lines.append(text_tag("moreImages", more_images))

    lines.append(numeric_tag("price", price))

    if compare_at_price and float(compare_at_price) > float(price):
        lines.append(numeric_tag("regularPrice", compare_at_price))

    lines.append(text_tag("curCode", DEFAULT_CURRENCY, use_cdata=False))
    lines.append(text_tag("stock", DEFAULT_STOCK, use_cdata=False))
    lines.append(numeric_tag("quantity", int(variant["inventoryQuantity"])))
    lines.append(text_tag("fileUnder", limit_text(category, 400)))
    lines.append(text_tag("brand", brand))

    if ean:
        lines.append(text_tag("EAN", ean))

    lines.append(text_tag("productCode", sku))
    lines.append(text_tag("condition", "new", use_cdata=False))
    lines.append(numeric_tag("deliveryCost", DELIVERY_COST))
    lines.append(numeric_tag("deliveryTimeMin", DELIVERY_TIME_MIN))
    lines.append(numeric_tag("deliveryTimeMax", DELIVERY_TIME_MAX))

    if group_id:
        lines.append(text_tag("groupId", limit_text(group_id, 50)))
    else:
        lines.append(text_tag("groupId", "", use_cdata=False))

    lines.extend(build_attributes_xml(variant))

    lines.append("</Item>")

    return "\n".join(lines)


def build_xml(variants: List[Dict[str, Any]]) -> str:
    included_count = 0
    skipped_count = 0

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<CNJExport>",
    ]

    for variant in variants:
        if not should_include_variant(variant):
            skipped_count += 1
            continue

        parts.append(build_item_xml(variant))
        included_count += 1

    parts.append("</CNJExport>")

    print(f"Included variants: {included_count}")
    print(f"Skipped variants: {skipped_count}")

    return "\n".join(parts) + "\n"


def write_xml(xml_content: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml_content, encoding="utf-8")

    print(f"XML written to: {path}")


def main() -> int:
    require_env()

    variants = fetch_variants()
    print(f"Fetched variants: {len(variants)}")

    attach_translations(variants, FEED_LOCALE)

    xml_content = build_xml(variants)
    write_xml(xml_content, OUTPUT_PATH)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
