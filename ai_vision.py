"""AI vision helper using Kimi (Moonshot AI) to explain images.

Replaces the old Google Gemini calls. The API is OpenAI-compatible, so we just
POST to the Moonshot chat/completions endpoint with an image + prompt.

For speed, the image is downscaled in-memory before being sent to Kimi (the AI
does not need a huge image to read the content). The original file on disk is
left untouched, so the final video stays full HD.
"""
import os
import io
import base64
import requests
from PIL import Image

KIMI_BASE_URL = 'https://api.moonshot.ai/v1'
KIMI_VISION_MODEL = 'moonshot-v1-8k-vision-preview'
MAX_DIM = 1280  # max width/height sent to Kimi -> fewer tokens, much faster


def explain_image(image_path, prompt):
    """Send an image + prompt to Kimi's vision model and return the text reply."""
    api_key = os.getenv('KIMI_API_KEY')
    if not api_key:
        raise RuntimeError("KIMI_API_KEY is not set in the .env file")

    # Downscale a copy in-memory (the on-disk file used for the HD video is untouched)
    img = Image.open(image_path)
    if img.mode != 'RGB':
        img = img.convert('RGB')
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM))
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()

    resp = requests.post(
        f'{KIMI_BASE_URL}/chat/completions',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        json={
            'model': KIMI_VISION_MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                    {'type': 'text', 'text': prompt},
                ],
            }],
            'temperature': 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()['choices'][0]['message']['content'].strip()
