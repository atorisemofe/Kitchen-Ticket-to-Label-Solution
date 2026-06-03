import os
from dotenv import load_dotenv
load_dotenv()

import base64
from typing import List
from pydantic import BaseModel
from openai import OpenAI

# =========================
# Schema
# =========================

class OrderItem(BaseModel):
    customer: str
    order_number: str
    pickup_time: str
    quantity: int
    item: str
    modifiers: List[str]

class OrderResponseSchema(BaseModel):
    orders: List[OrderItem]

# =========================
# Client
# =========================

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
MODEL_NAME = os.getenv("OPENAI_MODEL")

# =========================
# Helpers
# =========================

def encode_image(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

# =========================
# Main Parsing Function
# =========================

def parse_receipt_with_openai(image_path: str) -> List[dict]:

    base64_image = encode_image(image_path)

    response = client.chat.completions.parse(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a strict data extraction OCR tool. "
                    "Follow layout hierarchy rules:\n"
                    "1. ITEM = primary product.\n"
                    "2. MODIFIER = customization.\n"
                    "3. Modifiers under an item must be grouped under that item."
                )
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Extract all order details from this receipt image."
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{base64_image}",
                            "detail": "low"
                        }
                    }
                ]
            }
        ],
        response_format=OrderResponseSchema,
    )

    parsed = response.choices[0].message.parsed

    # Convert to plain dict list
    return [order.dict() for order in parsed.orders]