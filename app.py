import json
import os
import time
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse
from google import genai
from google.genai import types
from playwright.async_api import async_playwright

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(
    api_key=GEMINI_API_KEY, http_options={"api_version": "v1beta"}
)

app = FastAPI()


def extract_product_info(image_bytes: bytes, mime_type: str) -> dict:
  prompt = """
    이 사진 속 코스트코 제품을 온라인몰에서 검색하기 위한 정보를 추출해줘.
    반드시 아래 JSON 형식으로만 응답해:
    {
      "item_number": "사진에 6~7자리 코스트코 상품번호가 보이면 숫자만 추출, 없으면 null",
      "search_keyword": "브랜드명과 핵심 제품명 (한국 코스트코 온라인몰 검색용 키워드)"
    }
    """
  models_to_try = [
      "gemini-3.5-flash-lite",
      "gemini-3.1-flash-lite",
      "gemini-3.8-flash",
      "gemini-3.6-flash",
  ]
  last_err = None

  for model_name in models_to_try:
    try:
      print(f"   -> Gemini 호출 시도: {model_name}")
      response = client.models.generate_content(
          model=model_name,
          contents=[
              types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
              prompt,
          ],
          config=types.GenerateContentConfig(
              response_mime_type="application/json"
          ),
      )
      return json.loads(response.text)
    except Exception as e:
      print(f"   -> {model_name} 실패 ({e}), 다음 모델로 전환...")
      last_err = e
      time.sleep(1)

  raise last_err


async def check_costco_stock(query: str) -> list:
  if not query:
    return []

  async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
            viewport={"width": 1280, "height": 800},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

    search_url = f"https://www.costco.co.kr/search?text={query}"
    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(3000)

    candidate_selectors = [
        ".product-list-container .product-item",
        ".product-listing .product-tile",
        "li.product-item",
        "[class*='product-item']",
        "[class*='product-tile']",
    ]

    items_locator = None
    for sel in candidate_selectors:
      loc = page.locator(sel)
      if await loc.count() > 0:
        items_locator = loc
        break

    results = []
    seen = set()
    if items_locator is not None:
      count = min(await items_locator.count(), 6)
      for i in range(count):
        item = items_locator.nth(i)
        raw_text = (await item.inner_text()).strip()
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]

        ignore_keywords = (
            "배송비",
            "100g당",
            "★",
            "수량",
            "카트에 담기",
            "상품 비교하기",
            "Costco Grocery",
            "더 많은 옵션",
        )

        # 실제 판매가 (단위가격 '100g당' 및 '원'이 없는 줄 제외)
        price = next(
            (l for l in lines if "원" in l and "100g당" not in l),
            "가격 정보 없음",
        )

        # 실제 상품명 후보 추출 (가격/뱃지/버튼 문구 제외)
        title_candidates = [
            l
            for l in lines
            if "원" not in l and not any(k in l for k in ignore_keywords)
        ]
        title = title_candidates[0] if title_candidates else "상품명 확인 불가"

        # 검색어 키워드 중 하나라도 포함되지 않은 무관한 상품(예: 골드바) 제외
        query_words = [w for w in query.split() if len(w) >= 2]
        if query_words and not any(
            w.lower() in f"{title} {raw_text}".lower() for w in query_words
        ):
          continue

        is_sold_out = ("품절" in raw_text) or ("일시품절" in raw_text)

        combined_text = f"{title} {price} {raw_text}".lower()
        if any(k in combined_text for k in ["warehouse", "매장", "창고형", "오프라인"]):
          channel = "매장"
        else:
          channel = "쇼핑몰"

        dedup_key = (title, price, channel)
        if dedup_key in seen:
          continue
        seen.add(dedup_key)

        results.append({
            "title": title,
            "price": price,
            "status": "품절" if is_sold_out else "재고 있음 / 확인 필요",
            "channel": channel,
        })

    await browser.close()
    return results


@app.get("/", response_class=HTMLResponse)
def home():
  with open("index.html", "r", encoding="utf-8") as f:
    return f.read()


@app.post("/check")
async def check_image(file: UploadFile = File(...)):
  image_bytes = await file.read()
  mime_type = file.content_type or "image/jpeg"

  info = extract_product_info(image_bytes, mime_type)
  query = info.get("item_number") or info.get("search_keyword") or ""
  products = await check_costco_stock(str(query))

  return {
      "extracted": info,
      "query_used": query,
      "products": products,
  }