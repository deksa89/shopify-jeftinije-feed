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

# Public storefront domain used when Shopify does not return onlineStoreUrl.
PUBLIC_STORE_DOMAIN = os.getenv("PUBLIC_STORE_DOMAIN", "www.luvmechanics.com")

OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "docs/jeftinije.xml"))

# Empty = default Shopify language.
# Example:
# FEED_LOCALE=""   -> English/default feed
# FEED_LOCALE="hr" -> Croatian translated feed
FEED_LOCALE = os.getenv("FEED_LOCALE", "").strip()

MIN_QUANTITY = int(os.getenv("MIN_QUANTITY", "3"))
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "Erotska pomagala")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")

# Jeftinije.hr supports: in stock, preorder, out of stock.
# According to their spec:
# - "in stock" means product is dispatched no later than 1 day after order
# - "preorder" means dispatched within 2-10 working days
DEFAULT_STOCK = os.getenv("DEFAULT_STOCK", "preorder")

DELIVERY_COST = os.getenv("DELIVERY_COST", "6.90")
DELIVERY_TIME_MIN = os.getenv("DELIVERY_TIME_MIN", "4")
DELIVERY_TIME_MAX = os.getenv("DELIVERY_TIME_MAX", "8")

# If true, products without valid EAN-13 are excluded.
# Jeftinije says EAN is required when product has manufacturer EAN.
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
        description
        descriptionHtml
        status
        onlineStoreUrl
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

        # Some Shopify resources, especially variants, may not be available
        # through translatableResource even though they are returned by productVariants.
        # We skip them and fall back to the default value.
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

        # We use the value if it exists. Even if "outdated" is true,
        # it is usually still better than falling back to English.
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


def strip_html(value: Optional[str]) -> str:
    if not value:
        return ""

    value = html.unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()

    return value


def clean_html_description(value: Optional[str], fallback_text: Optional[str] = None) -> str:
    """
    Jeftinije.hr wants description in HTML code without styling.
    Shopify descriptionHtml is already HTML, so we preserve basic HTML.
    CDATA will protect HTML tags from breaking XML.
    """
    if not value:
        fallback = collapse_whitespace(fallback_text)
        return f"<p>{fallback}</p>" if fallback else ""

    description = str(value)

    # Remove risky/unnecessary tags if any app injected them.
    description = re.sub(
        r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>",
        "",
        description,
        flags=re.I,
    )
    description = re.sub(
        r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>",
        "",
        description,
        flags=re.I,
    )

    # Remove HTML comments, including Microsoft Word / Mso conditional comments.
    description = re.sub(r"<!--.*?-->", "", description, flags=re.S)

    # Remove all HTML attributes.
    # Example:
    # <p data-start="1" class="x"> -> <p>
    # <strong style="..."> -> <strong>
    # <li data-section-id="..."> -> <li>
    description = re.sub(
        r"<([a-zA-Z][a-zA-Z0-9]*)(?:\s+[^<>]*?)?\s*/?>",
        lambda match: f"<{match.group(1).lower()}>",
        description,
    )

    description = re.sub(r"\s+", " ", description).strip()

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


def translated_option_value(variant: Dict[str, Any], option_name: str, original_value: str) -> str:
    """
    Shopify can expose variant title translations, but selected option translations
    may not always be returned as simple keys. This keeps the original value as fallback.
    """
    translations = variant.get("_translations") or {}

    possible_keys = [
        option_name,
        option_name.lower(),
        f"option_{option_name}",
        f"option_{option_name.lower()}",
        original_value,
    ]

    for key in possible_keys:
        if key in translations and translations[key]:
            return translations[key]

    return original_value


def limit_text(value: str, max_length: int) -> str:
    value = value.strip()

    if len(value) <= max_length:
        return value

    return value[:max_length].rstrip()


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

    # Exclude restricted GS1 ranges mentioned in the Jeftinije.hr spec:
    # 02, 04, 2 and coupon ranges 98-99.
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


def more_image_urls(variant: Dict[str, Any]) -> str:
    urls = all_image_urls(variant)

    if len(urls) <= 1:
        return ""

    return ",".join(urls[1:])


def selected_options_map(variant: Dict[str, Any]) -> Dict[str, str]:
    options = {}

    for option in variant.get("selectedOptions") or []:
        name = collapse_whitespace(option.get("name")).lower()
        original_value = collapse_whitespace(option.get("value"))
        translated_value = translated_option_value(variant, name, original_value)

        if name and translated_value:
            options[name] = translated_value

    return options


def variant_color(variant: Dict[str, Any]) -> str:
    options = selected_options_map(variant)

    for key in ("color", "colour", "boja"):
        if key in options:
            return limit_text(options[key], 40)

    # Fallback: try to infer color from variant title after dash/name.
    title = collapse_whitespace(translated_variant_title(variant))
    known_colors = {
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
        "teal": "tirkizna",
        "turquoise": "tirkizna",
        "fuchsia": "fuksija",
        "indigo": "indigo",
    }

    lower_title = title.lower()

    for english, croatian in known_colors.items():
        if english in lower_title:
            return croatian if FEED_LOCALE == "hr" else english

    return ""


def variant_size(variant: Dict[str, Any]) -> str:
    options = selected_options_map(variant)

    for key in ("size", "veličina", "velicina"):
        if key in options:
            return limit_text(options[key], 40)

    return ""


def variant_name(variant: Dict[str, Any]) -> str:
    product = variant.get("product") or {}

    product_title = limit_text(collapse_whitespace(translated_product_title(product)), 170)
    variant_title = collapse_whitespace(translated_variant_title(variant))

    if variant_title and variant_title.lower() != "default title":
        return limit_text(f"{product_title}-{variant_title}", 200)

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
    """
    Safe CDATA wrapper. Handles rare occurrence of ]]>
    inside source text.
    """
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

    lines = []
    lines.append("\t<attributes>")
    lines.append(text_tag("gender", "", indent=2, use_cdata=False))
    lines.append(text_tag("color", color, indent=2, use_cdata=True))
    lines.append(text_tag("size", size, indent=2, use_cdata=True))
    lines.append(text_tag("ageGroup", "adult", indent=2, use_cdata=False))

    attribute_values = {
        "Brand": brand,
        "SKU": sku,
        "Kategorija": DEFAULT_CATEGORY,
    }

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
        lines.append(text_tag("moreImages", limit_text(more_images, 1000)))

    lines.append(numeric_tag("price", price))

    if compare_at_price and float(compare_at_price) > float(price):
        lines.append(numeric_tag("regularPrice", compare_at_price))

    lines.append(text_tag("curCode", DEFAULT_CURRENCY, use_cdata=False))
    lines.append(text_tag("stock", DEFAULT_STOCK, use_cdata=False))
    lines.append(numeric_tag("quantity", int(variant["inventoryQuantity"])))
    lines.append(text_tag("fileUnder", limit_text(DEFAULT_CATEGORY, 400)))
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
