from flask import Flask, render_template, request, jsonify
import os
import requests
from dotenv import load_dotenv
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.campaign import Campaign
from facebook_business.adobjects.adset import AdSet
from facebook_business.adobjects.adcreative import AdCreative
from facebook_business.adobjects.ad import Ad
from openai import OpenAI
import threading
import json
import traceback

load_dotenv()

app = Flask(__name__)

# ---------- FACEBOOK ADS CONFIG ----------
FB_ACCESS_TOKEN = os.getenv("SYSTEM_USER_TOKEN")
FB_PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
AD_ACCOUNT_ID = (
    os.getenv("FB_AD_ACCOUNT_ID").replace("act_", "")
    if os.getenv("FB_AD_ACCOUNT_ID")
    else ""
)
FB_PAGE_ID = os.getenv("FB_PAGE_ID")
FB_PIXEL_ID = os.getenv("FB_PIXEL_ID")
PRODUCT_CATALOG_ID = os.getenv("FB_PRODUCT_CATALOG_ID")
API_VERSION = "v21.0"  # Graph API version

print("\n" + "=" * 70)
print("FACEBOOK ADS CATALOG CONFIG")
print("=" * 70)
print(f"FB_ACCESS_TOKEN: {'✅ Set' if FB_ACCESS_TOKEN else '❌ Missing'}")
print(f"AD_ACCOUNT_ID: {AD_ACCOUNT_ID if AD_ACCOUNT_ID else '❌ Missing'}")
print(f"FB_PAGE_ID: {FB_PAGE_ID if FB_PAGE_ID else '❌ Missing'}")
print(f"FB_PIXEL_ID: {FB_PIXEL_ID if FB_PIXEL_ID else '❌ Missing'}")
print(
    f"PRODUCT_CATALOG_ID: {PRODUCT_CATALOG_ID if PRODUCT_CATALOG_ID else '❌ Missing'}"
)
print("=" * 70 + "\n")

if FB_ACCESS_TOKEN:
    FacebookAdsApi.init(access_token=FB_ACCESS_TOKEN)
    account = AdAccount(f"act_{AD_ACCOUNT_ID}")
else:
    account = None

# ---------- OPENAI / GPT CONFIG ----------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1")

if OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None

# ---------- RETARGETING AUDIENCE CONFIG ----------
# Example in .env:
# FB_RETARGETING_CA_IDS=1234567890,0987654321
RETARGETING_CA_IDS = [
    ca_id.strip()
    for ca_id in os.getenv("FB_RETARGETING_CA_IDS", "").split(",")
    if ca_id.strip()
]

# Global status
catalog_campaign_status = {
    "progress": 0,
    "message": "Ready",
    "campaign_id": None,
}


# ---------- HELPER FUNCTIONS ----------


def get_instagram_user_id():
    """Get Instagram Business Account ID from Facebook Page"""
    access_token = FB_PAGE_ACCESS_TOKEN if FB_PAGE_ACCESS_TOKEN else FB_ACCESS_TOKEN
    url = f"https://graph.facebook.com/{API_VERSION}/{FB_PAGE_ID}"
    params = {"fields": "instagram_business_account", "access_token": access_token}

    try:
        response = requests.get(url, params=params)
        data = response.json()

        if "instagram_business_account" in data:
            return data["instagram_business_account"]["id"]

        # Fallback: Try the old method
        url = (
            f"https://graph.facebook.com/{API_VERSION}/{FB_PAGE_ID}/instagram_accounts"
        )
        params = {"fields": "id,username", "access_token": access_token}
        response = requests.get(url, params=params)
        data = response.json()

        if "data" in data and len(data["data"]) > 0:
            return data["data"][0]["id"]

    except Exception as e:
        print(f"❌ Error getting Instagram ID: {e}")

    return None


def get_catalog_products(catalog_id, limit=1000):
    """Fetch products from catalog"""
    try:
        print(f"\n📦 Fetching products from catalog {catalog_id}...")
        url = f"https://graph.facebook.com/{API_VERSION}/{catalog_id}/products"
        params = {
            "fields": "id,name,price,brand,product_type,retailer_id,availability,image_url,description,custom_label_0,custom_label_1,custom_label_2,custom_label_3,custom_label_4",
            "limit": limit,
            "access_token": FB_ACCESS_TOKEN,
        }

        all_products = []

        while url:
            response = requests.get(url, params=params)
            data = response.json()

            if "error" in data:
                error_msg = data["error"].get("message", "Unknown error")
                error_code = data["error"].get("code", "N/A")
                raise Exception(f"Facebook API Error {error_code}: {error_msg}")

            products = data.get("data", [])
            all_products.extend(products)

            # Check for next page
            url = data.get("paging", {}).get("next")
            params = {}  # URL already contains params

            print(f"✅ Retrieved {len(all_products)} products so far...")

            if len(all_products) >= limit:
                break

        print(f"✅ Total products retrieved: {len(all_products)}")
        return all_products[:limit]

    except Exception as e:
        print(f"❌ Error fetching catalog products: {e}")
        print(traceback.format_exc())
        raise


def get_unique_values(products, field):
    """Extract unique values for a field from products"""
    values = set()
    for product in products:
        if field in product and product[field]:
            value = str(product[field]).strip()
            if value and value.lower() not in ["none", "null", ""]:
                values.add(value)
    return sorted(list(values))


# ---------- ROUTES ----------


@app.route("/")
def index():
    return render_template("catalog_builder.html")


@app.route("/get_catalog_info", methods=["GET"])
def get_catalog_info():
    """Get catalog products and available filter values"""
    try:
        print(f"\n{'='*70}")
        print("GET_CATALOG_INFO ENDPOINT CALLED")
        print(f"{'='*70}")
        print(f"🔍 Catalog ID: {PRODUCT_CATALOG_ID}")

        if not PRODUCT_CATALOG_ID:
            error_msg = "No catalog ID configured. Please add FB_PRODUCT_CATALOG_ID to your .env file"
            print(f"❌ {error_msg}")
            return jsonify({"success": False, "error": error_msg}), 400

        # Get products
        products = get_catalog_products(PRODUCT_CATALOG_ID, limit=1000)
        print(f"✅ Found {len(products)} products")

        if len(products) == 0:
            print("⚠️ Warning: No products found in catalog")
            return jsonify(
                {
                    "success": True,
                    "catalog_id": PRODUCT_CATALOG_ID,
                    "total_products": 0,
                    "brands": [],
                    "categories": [],
                    "custom_labels_0": [],
                    "custom_labels_1": [],
                    "custom_labels_2": [],
                    "custom_labels_3": [],
                    "custom_labels_4": [],
                    "price_range": {"min": 0, "max": 10000},
                }
            )

        # Extract unique values for filters
        print("📊 Extracting filter values...")
        brands = get_unique_values(products, "brand")
        categories = get_unique_values(products, "product_type")
        custom_labels_0 = get_unique_values(products, "custom_label_0")
        custom_labels_1 = get_unique_values(products, "custom_label_1")
        custom_labels_2 = get_unique_values(products, "custom_label_2")
        custom_labels_3 = get_unique_values(products, "custom_label_3")
        custom_labels_4 = get_unique_values(products, "custom_label_4")

        print(f"   Brands: {len(brands)}")
        print(f"   Categories: {len(categories)}")
        print(f"   Custom Labels 0: {len(custom_labels_0)}")

        # Get price range
        prices = []
        for p in products:
            if p.get("price"):
                try:
                    price_str = str(p.get("price", "0"))
                    # Remove currency symbols and extract number
                    price_str = (
                        price_str.replace(",", "")
                        .replace("₹", "")
                        .replace("INR", "")
                        .replace("USD", "")
                        .replace("$", "")
                        .strip()
                    )
                    # Handle price format like "1234.56 INR"
                    price_str = price_str.split()[0]
                    if price_str:
                        prices.append(float(price_str))
                except (ValueError, TypeError):
                    continue

        price_range = {
            "min": int(min(prices)) if prices else 0,
            "max": int(max(prices)) if prices else 10000,
        }

        print(f"   Price range: ₹{price_range['min']} - ₹{price_range['max']}")
        print(f"{'='*70}\n")

        result = {
            "success": True,
            "catalog_id": PRODUCT_CATALOG_ID,
            "total_products": len(products),
            "brands": brands,
            "categories": categories,
            "custom_labels_0": custom_labels_0,
            "custom_labels_1": custom_labels_1,
            "custom_labels_2": custom_labels_2,
            "custom_labels_3": custom_labels_3,
            "custom_labels_4": custom_labels_4,
            "price_range": price_range,
        }

        return jsonify(result)

    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"\n{'='*70}")
        print("❌ ERROR IN GET_CATALOG_INFO")
        print(f"{'='*70}")
        print(error_trace)
        print(f"{'='*70}\n")
        return jsonify({"success": False, "error": str(e), "trace": error_trace}), 500


@app.route("/list_product_sets", methods=["GET"])
def list_product_sets():
    """List all existing product sets"""
    try:
        print(f"\n📋 Listing product sets for catalog {PRODUCT_CATALOG_ID}...")
        url = f"https://graph.facebook.com/{API_VERSION}/{PRODUCT_CATALOG_ID}/product_sets"
        params = {
            "fields": "id,name,product_count,filter",
            "access_token": FB_ACCESS_TOKEN,
        }
        response = requests.get(url, params=params)
        data = response.json()

        if "error" in data:
            print(f"❌ Error listing product sets: {data['error']}")
            return jsonify(
                {
                    "success": False,
                    "error": data["error"].get("message", "Unknown error"),
                }
            )

        product_sets = data.get("data", [])
        print(f"✅ Found {len(product_sets)} product sets")

        return jsonify({"success": True, "product_sets": product_sets})

    except Exception as e:
        print(f"❌ Error: {e}")
        print(traceback.format_exc())
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/create_catalog_campaign", methods=["POST"])
def create_catalog_campaign():
    """Create Advantage+ catalog campaign"""
    data = request.json
    thread = threading.Thread(target=create_catalog_campaign_background, args=(data,))
    thread.start()
    return jsonify({"success": True, "message": "Campaign creation started"})


@app.route("/generate_ai_text", methods=["POST"])
def generate_ai_text():
    """Generate AI ad message using GPT"""
    if not openai_client:
        return (
            jsonify(
                {
                    "success": False,
                    "error": "OPENAI_API_KEY is not configured on the server.",
                }
            ),
            500,
        )

    try:
        payload = request.get_json(force=True) or {}
        campaign_name = (payload.get("campaign_name") or "").strip()
        brands = payload.get("brands") or []
        categories = payload.get("categories") or []
        price_min = payload.get("price_min")
        price_max = payload.get("price_max")

        context_parts = []
        if campaign_name:
            context_parts.append(f"Campaign name: {campaign_name}")
        if brands:
            context_parts.append(f"Brands: {', '.join(brands)}")
        if categories:
            context_parts.append(f"Categories: {', '.join(categories)}")
        if price_min or price_max:
            context_parts.append(
                f"Price range (INR): {price_min or '-'} to {price_max or '-'}"
            )

        brief = (
            "; ".join(context_parts)
            if context_parts
            else "Generic InArt bathroom & kitchen fittings campaign in India."
        )

        prompt = (
            "You are an expert Meta Ads copywriter for an Indian D2C brand called InArt, "
            "selling bathroom and kitchen fittings like faucets, wash basins, showers, and toilets. "
            "Write ONE primary ad text for an Advantage+ Catalog campaign. "
            "Tone: sales-focused but natural, simple Indian English, 2–3 short sentences, can use 2–3 relevant emojis. "
            "Don't add headlines or titles, only the primary text. "
            "Don't use line breaks at the start or end. "
            f"Here is the campaign context: {brief}"
        )

        completion = openai_client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            max_output_tokens=120,
        )

        # Extract text from Responses API output
        try:
            text = completion.output[0].content[0].text.strip()
        except Exception:
            text = str(completion)

        return jsonify({"success": True, "text": text})

    except Exception as e:
        print("❌ Error in generate_ai_text:", e)
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- CORE LOGIC ----------


def create_catalog_campaign_background(config):
    """Background task to create Advantage+ catalog campaign"""
    global catalog_campaign_status

    try:
        catalog_campaign_status = {
            "progress": 5,
            "message": "Validating configuration...",
        }

        if not account:
            catalog_campaign_status = {
                "progress": -1,
                "message": "Facebook account not initialized. Check SYSTEM_USER_TOKEN / FB_AD_ACCOUNT_ID.",
            }
            return

        # Validate required fields
        if not PRODUCT_CATALOG_ID:
            catalog_campaign_status = {
                "progress": -1,
                "message": "Product Catalog ID is missing",
            }
            return

        catalog_campaign_status = {
            "progress": 10,
            "message": "Getting Instagram account...",
        }

        instagram_user_id = get_instagram_user_id()
        if not instagram_user_id:
            catalog_campaign_status = {
                "progress": -1,
                "message": "No Instagram account found. Please connect an Instagram Business account to your Facebook Page.",
            }
            return

        catalog_campaign_status = {
            "progress": 20,
            "message": "Creating/selecting product set...",
        }

        # Handle product set
        if config.get("use_existing_set") and config.get("existing_set_id"):
            product_set_id = config["existing_set_id"]
            print(f"✅ Using existing product set: {product_set_id}")
        else:
            # Build filter and create product set
            filter_rules = build_product_filter(config.get("filters", {}))
            product_set_id = create_product_set(
                PRODUCT_CATALOG_ID,
                config.get("product_set_name", "Auto Product Set"),
                filter_rules,
            )

        if not product_set_id:
            catalog_campaign_status = {
                "progress": -1,
                "message": "Failed to create/select product set",
            }
            return

        catalog_campaign_status = {"progress": 40, "message": "Creating campaign..."}

        # Create Advantage+ Shopping Campaign
        campaign_id = create_advantage_plus_campaign(
            config.get("campaign_name", "Advantage+ Catalog Campaign"),
            config.get("daily_budget", 50000),  # 500 INR in paisa
        )

        catalog_campaign_status = {"progress": 60, "message": "Creating ad sets..."}

        # --- Budget split: ~90% Broad, 10% Retargeting (if retargeting audiences exist) ---
        total_daily_budget = config.get("daily_budget", 50000)  # in paisa
        retargeting_budget = 0
        broad_budget = total_daily_budget

        retargeting_adset_id = None
        retargeting_ad_id = None

        if RETARGETING_CA_IDS:
            # Allocate 10% to retargeting, with a sensible minimum (₹100 = 10000 paisa)
            retargeting_budget = max(int(total_daily_budget * 0.10), 10000)
            broad_budget = max(total_daily_budget - retargeting_budget, 0)
            print(
                f"💰 Budget split -> Broad: {broad_budget} | Retargeting: {retargeting_budget}"
            )

        # 1) Broad Ad Set (Prospecting / mixed)
        adset_id = create_advantage_adset(
            campaign_id,
            product_set_id,
            broad_budget,
            config.get("targeting", {}),
        )

        # 2) Optional Retargeting Ad Set (Warm traffic)
        if retargeting_budget > 0:
            retargeting_adset_id = create_retarg_adset(
                campaign_id,
                product_set_id,
                retargeting_budget,
                RETARGETING_CA_IDS,
            )

        catalog_campaign_status = {
            "progress": 80,
            "message": "Creating ad creative...",
        }

        creative_id = create_advantage_creative(
            product_set_id,
            instagram_user_id,
            config.get("ad_message", "Check out {{product.name}} - Shop Now! 🛍️"),
        )

        catalog_campaign_status = {"progress": 90, "message": "Creating ads..."}

        # Main ad for broad ad set
        ad_id = create_ad(
            adset_id, creative_id, config.get("ad_name", "Advantage+ Catalog Ad")
        )

        # Optional ad for retargeting ad set (same creative)
        if retargeting_adset_id:
            retargeting_ad_id = create_ad(
                retargeting_adset_id,
                creative_id,
                config.get("ad_name", "Advantage+ Catalog Ad") + " - Retargeting",
            )

        catalog_campaign_status = {
            "progress": 100,
            "message": "✅ Campaign created successfully!",
            "campaign_id": campaign_id,
            "adset_id": adset_id,
            "ad_id": ad_id,
            "product_set_id": product_set_id,
            "retargeting_adset_id": retargeting_adset_id,
            "retargeting_ad_id": retargeting_ad_id,
        }

    except Exception as e:
        error_trace = traceback.format_exc()
        print(f"❌ Campaign creation error:\n{error_trace}")
        catalog_campaign_status = {
            "progress": -1,
            "message": f"Error: {str(e)}",
            "error": error_trace,
        }


def build_product_filter(filters):
    """Build Facebook product filter from UI selections - Updated for 2025"""
    conditions = []

    # Brand filter
    if filters.get("brands"):
        if len(filters["brands"]) == 1:
            conditions.append({"brand": {"eq": filters["brands"][0]}})
        else:
            conditions.append({"brand": {"is_any": filters["brands"]}})

    # Category filter
    if filters.get("categories"):
        if len(filters["categories"]) == 1:
            conditions.append(
                {"product_type": {"i_contains": filters["categories"][0]}}
            )
        else:
            or_conditions = [
                {"product_type": {"i_contains": cat}} for cat in filters["categories"]
            ]
            conditions.append({"or": or_conditions})

    # Price filters
    if filters.get("price_min") is not None and filters.get("price_min") > 0:
        conditions.append({"price": {"gte": str(filters["price_min"])}})
    if filters.get("price_max") is not None and filters.get("price_max") > 0:
        conditions.append({"price": {"lte": str(filters["price_max"])}})

    # Availability filter (if needed in future)
    if filters.get("availability"):
        conditions.append({"availability": {"eq": filters["availability"]}})

    # Custom labels
    for i in range(5):
        label_key = f"custom_label_{i}"
        if filters.get(label_key):
            conditions.append({label_key: {"is_any": filters[label_key]}})

    # Specific product IDs - NOTE: retailer_id must match EXACTLY what's in catalog
    if filters.get("product_ids") and len(filters.get("product_ids")) > 0:
        product_ids = [
            str(pid).strip() for pid in filters["product_ids"] if str(pid).strip()
        ]
        if len(product_ids) > 0:
            print(f"🔍 Filtering by product IDs: {product_ids}")
            if len(product_ids) == 1:
                conditions.append({"retailer_id": {"eq": product_ids[0]}})
            else:
                conditions.append({"retailer_id": {"is_any": product_ids}})

    if len(conditions) == 0:
        print("⚠️ No filter conditions provided - will include ALL products")
        return None
    elif len(conditions) == 1:
        return conditions[0]
    else:
        return {"and": conditions}


def create_product_set(catalog_id, set_name, filter_rules):
    """Create product set with filters"""
    try:
        if not set_name:
            set_name = "Auto Product Set"

        # If no filter rules, create without filter to include all products
        if not filter_rules:
            print("📋 Creating product set with ALL products (no filter)")
            url = f"https://graph.facebook.com/{API_VERSION}/{catalog_id}/product_sets"
            params = {"name": set_name, "access_token": FB_ACCESS_TOKEN}
        else:
            print(
                f"📋 Creating product set with filter: {json.dumps(filter_rules, indent=2)}"
            )

            # First, validate that the filter will match some products
            test_url = f"https://graph.facebook.com/{API_VERSION}/{catalog_id}/products"
            test_params = {
                "filter": json.dumps(filter_rules),
                "limit": 1,
                "access_token": FB_ACCESS_TOKEN,
            }

            test_response = requests.get(test_url, params=test_params)
            test_data = test_response.json()

            if "data" in test_data and len(test_data["data"]) == 0:
                print(
                    "⚠️ Warning: Filter matches 0 products. Creating set with all products instead."
                )
                url = f"https://graph.facebook.com/{API_VERSION}/{catalog_id}/product_sets"
                params = {
                    "name": set_name + " (All Products)",
                    "access_token": FB_ACCESS_TOKEN,
                }
            else:
                print("✅ Filter matches products. Creating product set...")
                url = f"https://graph.facebook.com/{API_VERSION}/{catalog_id}/product_sets"
                params = {
                    "name": set_name,
                    "filter": json.dumps(filter_rules),
                    "access_token": FB_ACCESS_TOKEN,
                }

        response = requests.post(url, data=params)
        data = response.json()

        if "error" in data:
            error_msg = data["error"].get("message", "Unknown error")
            print(f"❌ Error creating product set: {error_msg}")

            # If still getting empty set error, create without filter as last resort
            if "empty product set" in error_msg.lower():
                print("🔄 Attempting to create set without filters...")
                fallback_params = {
                    "name": set_name + " (All Products - Auto)",
                    "access_token": FB_ACCESS_TOKEN,
                }
                fallback_response = requests.post(
                    f"https://graph.facebook.com/{API_VERSION}/{catalog_id}/product_sets",
                    data=fallback_params,
                )
                fallback_data = fallback_response.json()

                if "error" not in fallback_data:
                    print(f"✅ Fallback product set created: {fallback_data.get('id')}")
                    return fallback_data.get("id")

            return None

        print(f"✅ Product set created: {data.get('id')}")
        return data.get("id")

    except Exception as e:
        print(f"❌ Error creating product set: {e}")
        traceback.print_exc()
        return None


def create_advantage_plus_campaign(campaign_name, daily_budget):
    """
    Create Advantage+ Shopping Campaign
    Updated for 2025 - Uses OUTCOME_SALES objective
    """
    try:
        campaign = account.create_campaign(
            fields=[],
            params={
                "name": campaign_name,
                "objective": "OUTCOME_SALES",
                "status": Campaign.Status.paused,
                "special_ad_categories": [],
                "is_adset_budget_sharing_enabled": False,
            },
        )

        campaign_id = campaign.get("id")
        print(f"✅ Campaign created: {campaign_id}")
        return campaign_id

    except Exception as e:
        print(f"❌ Error creating campaign: {e}")
        raise


def create_advantage_adset(campaign_id, product_set_id, daily_budget, targeting_config):
    """
    Create BROAD Advantage+ Ad Set with:
    - IN only
    - FB + IG placements (no Audience Network / Messenger)
    - Purchase optimization
    - Slightly higher minimum age to reduce fake CODs
    - FIXED: Removed deprecated 'video_feeds' placement
    """
    try:
        targeting = {
            "geo_locations": {
                "countries": ["IN"],
                "location_types": ["home", "recent"],
            },
            "age_min": targeting_config.get("age_min", 21),
            "age_max": targeting_config.get("age_max", 55),
            # 2025 APPROVED PLATFORM SETTINGS
            "publisher_platforms": ["facebook", "instagram"],
            # FB placements - REMOVED 'video_feeds' (deprecated)
            "facebook_positions": [
                "feed",
                "marketplace",
                "right_hand_column",
            ],
            # IG placements
            "instagram_positions": [
                "stream",
                "story",
                "explore",
                "reels",
            ],
        }

        # Gender filter (if user wants to narrow)
        if targeting_config.get("gender") and targeting_config.get("gender") != "all":
            gender_map = {"male": [1], "female": [2]}
            targeting["genders"] = gender_map.get(targeting_config["gender"], [1, 2])

        params = {
            "name": "Advantage+ AdSet - Broad",
            "campaign_id": campaign_id,
            "daily_budget": daily_budget,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": targeting,
            "promoted_object": {
                "pixel_id": FB_PIXEL_ID,
                "custom_event_type": "PURCHASE",
                "product_set_id": product_set_id,
            },
            "status": AdSet.Status.paused,
        }

        ad_set = account.create_ad_set(fields=[], params=params)

        adset_id = ad_set.get("id")
        print(f"✅ Broad ad set created: {adset_id}")
        return adset_id

    except Exception as e:
        print(f"❌ Error creating broad ad set: {e}")
        raise


def create_retarg_adset(campaign_id, product_set_id, daily_budget, custom_audience_ids):
    """
    Create Retargeting Ad Set (warm traffic):
    - Uses custom audiences (website visitors / engagers / purchasers)
    - Same clean placements as broad ad set
    - FIXED: Removed deprecated 'video_feeds' placement
    """
    if not custom_audience_ids:
        return None

    try:
        targeting = {
            "geo_locations": {
                "countries": ["IN"],
                "location_types": ["home", "recent"],
            },
            "age_min": 18,
            "age_max": 65,
            # Same clean placement logic (no Audience Network / Messenger simply by not including them)
            "publisher_platforms": ["facebook", "instagram"],
            # REMOVED 'video_feeds' (deprecated)
            "facebook_positions": [
                "feed",
                "marketplace",
                "right_hand_column",
            ],
            "instagram_positions": [
                "stream",
                "story",
                "explore",
                "reels",
            ],
            # Warm audiences
            "custom_audiences": [{"id": ca_id} for ca_id in custom_audience_ids],
        }

        params = {
            "name": "Advantage+ AdSet - Retargeting (Warm 10%)",
            "campaign_id": campaign_id,
            "daily_budget": daily_budget,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "OFFSITE_CONVERSIONS",
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "targeting": targeting,
            "promoted_object": {
                "pixel_id": FB_PIXEL_ID,
                "custom_event_type": "PURCHASE",
                "product_set_id": product_set_id,
            },
            "status": AdSet.Status.paused,
        }

        ad_set = account.create_ad_set(fields=[], params=params)
        adset_id = ad_set.get("id")
        print(f"✅ Retargeting ad set created: {adset_id}")
        return adset_id

    except Exception as e:
        print(f"❌ Error creating retargeting ad set: {e}")
        raise


def create_advantage_creative(product_set_id, instagram_user_id, message):
    """
    Create Advantage+ catalog creative with dynamic media enabled
    """
    try:
        template_data = {
            "call_to_action": {"type": "SHOP_NOW"},
            "message": message,  # Can use {{product.name}}, {{product.price}}, etc.
            "name": "{{product.name}}",
            "link": "https://inart.co.in/products/{{product.retailer_id}}?utm_source=facebook&utm_medium=catalog&utm_campaign={{campaign.name}}",
            "description": "{{product.description | truncatewords: 20}}",
        }

        object_story_spec = {
            "page_id": FB_PAGE_ID,
            "instagram_actor_id": instagram_user_id,
            "template_data": template_data,
        }

        params = {
            "name": "Advantage+ Catalog Creative",
            "object_story_spec": object_story_spec,
            "product_set_id": product_set_id,
        }

        creative = account.create_ad_creative(fields=[], params=params)

        creative_id = creative.get("id")
        print(f"✅ Creative created: {creative_id}")
        return creative_id

    except Exception as e:
        print(f"❌ Error creating creative: {e}")
        raise


def create_ad(adset_id, creative_id, ad_name):
    """Create the ad"""
    try:
        ad = account.create_ad(
            fields=[],
            params={
                "name": ad_name,
                "adset_id": adset_id,
                "creative": {"creative_id": creative_id},
                "status": Ad.Status.paused,
            },
        )

        ad_id = ad.get("id")
        print(f"✅ Ad created: {ad_id}")
        return ad_id

    except Exception as e:
        print(f"❌ Error creating ad: {e}")
        raise


@app.route("/catalog_status")
def get_catalog_status():
    """Get campaign creation status"""
    return jsonify(catalog_campaign_status)


if __name__ == "__main__":
    print("\n🚀 Starting Flask Catalog Campaign Builder...")
    print(f"📍 Access at: http://localhost:5000\n")
    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)
