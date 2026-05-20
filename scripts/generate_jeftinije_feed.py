import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional


SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2026-04")
SHOPIFY_STORE = os.getenv("SHOPIFY_STORE")
SHOPIFY_ADMIN_TOKEN = os.getenv("SHOPIFY_ADMIN_TOKEN")

OUTPUT_PATH = Path(os.getenv("OUTPUT_PATH", "docs/jeftinije.xml"))

MIN_QUANTITY = int(os.getenv("MIN_QUANTITY", "3"))
DEFAULT_CATEGORY = os.getenv("DEFAULT_CATEGORY", "Erotska pomagala")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "EUR")
DEFAULT_STOCK = os.getenv("DEFAULT_STOCK", "in stock")
DELIVERY_COST = os.getenv("DELIVERY_COST", "6.90")
DELIVERY_TIME_MIN = os.getenv("DELIVERY_TIME_MIN", "4")
DELIVERY_TIME_MAX = os.getenv("DELIVERY_TIME_MAX", "8")

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
      inventoryQuantity
      availableForSale
      image {
        url
      }
      product {
        title
        handle
        vendor
        description
        status
        onlineStoreUrl
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


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""

    value = html.unescape(value)

    # Safety: remove any HTML tags if Shopify description contains HTML.
    value = re.sub(r"<[^>]+>", " ", value)

    # Replace characters that caused issues in earlier feed tests.
    value = value.replace("&", " and ")
    value = value.replace("<", " less than ")
    value = value.replace(">", " ")

    value = re.sub(r"\s+", " ", value).strip()

    return value


def normalize_price(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return ""


def normalize_ean(value: Optional[str]) -> str:
    if not value:
        return ""

    digits = re.sub(r"\D", "", value)

    if len(digits) == 13:
        return digits

    return ""


def product_url(product: Dict[str, Any], variant_id: str) -> str:
    online_url = product.get("onlineStoreUrl")

    if online_url:
        separator = "&" if "?" in online_url else "?"
        return f"{online_url}{separator}variant={variant_id}"

    handle = product.get("handle")

    if handle:
        return f"https://www.luvmechanics.com/products/{handle}?variant={variant_id}"

    return ""


def image_url(variant: Dict[str, Any]) -> str:
    variant_image = variant.get("image") or {}

    if variant_image.get("url"):
        return variant_image["url"]

    product = variant.get("product") or {}
    media = product.get("featuredMedia") or {}
    preview = media.get("preview") or {}
    image = preview.get("image") or {}

    return image.get("url") or ""


def variant_name(variant: Dict[str, Any]) -> str:
    product = variant.get("product") or {}

    product_title = clean_text(product.get("title"))
    variant_title = clean_text(variant.get("title"))

    if variant_title and variant_title.lower() != "default title":
        return f"{product_title}-{variant_title}"

    return product_title


def should_include_variant(variant: Dict[str, Any]) -> bool:
    product = variant.get("product") or {}

    if product.get("status") != "ACTIVE":
        return False

    quantity = variant.get("inventoryQuantity")

    if quantity is None or int(quantity) < MIN_QUANTITY:
        return False

    price = normalize_price(variant.get("price"))

    if not price:
        return False

    if not variant.get("sku"):
        return False

    if not image_url(variant):
        return False

    if not product_url(product, numeric_id(variant["id"])):
        return False

    if REQUIRE_EAN and not normalize_ean(variant.get("barcode")):
        return False

    return True


def add_text(parent: ET.Element, tag: str, text: Any) -> ET.SubElement:
    child = ET.SubElement(parent, tag)
    child.text = "" if text is None else str(text)
    return child


def build_xml(variants: List[Dict[str, Any]]) -> ET.ElementTree:
    root = ET.Element("CNJExport")

    included_count = 0
    skipped_count = 0

    for variant in variants:
        if not should_include_variant(variant):
            skipped_count += 1
            continue

        product = variant["product"]
        variant_id = numeric_id(variant["id"])
        quantity = int(variant["inventoryQuantity"])
        ean = normalize_ean(variant.get("barcode"))

        item = ET.SubElement(root, "Item")

        add_text(item, "ID", variant_id)
        add_text(item, "name", variant_name(variant))
        add_text(item, "description", clean_text(product.get("description")))
        add_text(item, "link", product_url(product, variant_id))
        add_text(item, "mainImage", image_url(variant))
        add_text(item, "price", normalize_price(variant.get("price")))
        add_text(item, "brand", clean_text(product.get("vendor")) or "LuvMechanics")
        add_text(item, "productCode", clean_text(variant.get("sku")))
        add_text(item, "quantity", quantity)
        add_text(item, "fileUnder", DEFAULT_CATEGORY)
        add_text(item, "curCode", DEFAULT_CURRENCY)
        add_text(item, "stock", DEFAULT_STOCK)
        add_text(item, "deliveryCost", DELIVERY_COST)

        if ean:
            add_text(item, "EAN", ean)

        add_text(item, "deliveryTimeMin", DELIVERY_TIME_MIN)
        add_text(item, "deliveryTimeMax", DELIVERY_TIME_MAX)

        included_count += 1

    print(f"Included variants: {included_count}")
    print(f"Skipped variants: {skipped_count}")

    return ET.ElementTree(root)


def write_xml(tree: ET.ElementTree, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    ET.indent(tree, space="\t", level=0)
    tree.write(path, encoding="utf-8", xml_declaration=True)

    print(f"XML written to: {path}")


def main() -> int:
    require_env()

    variants = fetch_variants()
    print(f"Fetched variants: {len(variants)}")

    tree = build_xml(variants)
    write_xml(tree, OUTPUT_PATH)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
