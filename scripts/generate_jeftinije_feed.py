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

MIN_QUANTITY = int(os.getenv("MIN_QUANTITY", "3"))
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "Erotska pomagala")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")

# Jeftinije.hr supports: in stock, preorder, out of stock.
# If you dispatch within 1 day, use "in stock".
# If delivery/dispatch is usually 2-10 working days, "preorder" is closer to their definition.
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
    description = re.sub(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", "", description, flags=re.I)
    description = re.sub(r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>", "", description, flags=re.I)

    # Remove inline style attributes because spec says HTML without formatting.
    description = re.sub(r'\sstyle="[^"]*"', "", description, flags=re.I)
    description = re.sub(r"\sstyle='[^']*'", "", description, flags=re.I)

    description = description.strip()

    return description


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

    # Exclude restricted GS1 ranges mentioned in the Jeftinije.hr spec.
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
    online_url = product.get("onlineStoreUrl")

    if online_url:
        separator = "&" if "?" in online_url else "?"
        return f"{online_url}{separator}variant={variant_id}"

    handle = product.get("handle")

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
        value = collapse_whitespace(option.get("value"))

        if name and value:
            options[name] = value

    return options


def variant_color(variant: Dict[str, Any]) -> str:
    options = selected_options_map(variant)

    for key in ("color", "colour", "boja"):
        if key in options:
            return limit_text(options[key], 40)

    return ""


def variant_size(variant: Dict[str, Any]) -> str:
    options = selected_options_map(variant)

    for key in ("size", "veličina", "velicina"):
        if key in options:
            return limit_text(options[key], 40)

    return ""


def variant_name(variant: Dict[str, Any]) -> str:
    product = variant.get("product") or {}

    product_title = limit_text(collapse_whitespace(product.get("title")), 170)
    variant_title = collapse_whitespace(variant.get("title"))

    if variant_title and variant_title.lower() != "default title":
        return limit_text(f"{product_title}-{variant_title}", 200)

    return limit_text(product_title, 200)


def has_variant_title(variant: Dict[str, Any]) -> bool:
    title = collapse_whitespace(variant.get("title"))
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
        product.get("descriptionHtml"),
        fallback_text=product.get("description"),
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

    xml_content = build_xml(variants)
    write_xml(xml_content, OUTPUT_PATH)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
