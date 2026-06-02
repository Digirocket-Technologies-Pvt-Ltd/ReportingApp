"""DigiRocket DROK v5 — fine-tuned Qwen2.5-7B-Instruct.

Three run modes, controlled by env var DROK_MODE:

  - "api"     (recommended for prod): proxies to the canonical DigiRocket
              DROK app at https://drok.digirocket.io/api/chat. Inherits the
              standalone app's Pinecone RAG layer for free — answers about
              team members, case studies, etc. that aren't baked into the
              GGUF weights still come back correct. Default.
  - "ollama"  (dev / fallback): talks to a local Ollama daemon
              (http://localhost:11434). Setup once with
              `ollama pull hf.co/Digirocket/drok-v5:Q4_K_M`. Works offline
              but only answers what's in the fine-tuned weights — no RAG.
  - "hf"      : calls a Hugging Face Inference Endpoint (set DROK_ENDPOINT_URL).

The api mode also enables an automatic fallback to ollama when the
standalone endpoint is unreachable (set DROK_API_FALLBACK_OLLAMA=1) so the
chat keeps working even if drok.digirocket.io is down.

Env vars:
  DROK_MODE                 - "api" | "ollama" | "hf"  (default: api)
  DROK_API_URL              - standalone DROK endpoint
                              (default: https://drok.digirocket.io/api/chat)
  DROK_API_TIMEOUT          - seconds (default: 60)
  DROK_API_FALLBACK_OLLAMA  - "1" to fall back to local Ollama on api failure
  DROK_OLLAMA_HOST          - Ollama daemon URL (default: http://localhost:11434)
  DROK_OLLAMA_MODEL         - Ollama model tag
                              (default: hf.co/Digirocket/drok-v5:Q4_K_M)
  HF_TOKEN, DROK_ENDPOINT_URL  - HF mode only
"""
import os
import re
import requests

# ANSI escape sequence (terminal color codes) that occasionally leak into
# the API's plain-text response.
_ANSI_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Zero-width / BOM / directional control characters that show as boxes
# in an HTML chat widget.
_INVIS_RE = re.compile(
    '[​-‏‪-‮⁠-⁯﻿]'
)

# Any other C0 / C1 control char that isn't a tab/newline/CR.
_CTRL_RE = re.compile('[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]')


def _clean_text(s):
    if not s:
        return ''
    s = _ANSI_RE.sub('', s)
    s = _INVIS_RE.sub('', s)
    s = _CTRL_RE.sub('', s)
    # Collapse any 3+ consecutive newlines down to 2 (paragraph break).
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()

DROK_MODE = (os.getenv('DROK_MODE') or 'api').lower().strip()

# api mode
DROK_API_URL = (os.getenv('DROK_API_URL') or 'https://drok.digirocket.io/api/chat').strip()
DROK_API_TIMEOUT = int(os.getenv('DROK_API_TIMEOUT', '60'))
DROK_API_FALLBACK_OLLAMA = (os.getenv('DROK_API_FALLBACK_OLLAMA') or '1').strip() == '1'

# ollama mode
DROK_OLLAMA_HOST = (os.getenv('DROK_OLLAMA_HOST') or 'http://localhost:11434').rstrip('/')
DROK_OLLAMA_MODEL = os.getenv('DROK_OLLAMA_MODEL') or 'hf.co/Digirocket/drok-v5:Q4_K_M'

# hf mode
HF_TOKEN = (os.getenv('HF_TOKEN') or '').strip()
DROK_ENDPOINT_URL = (os.getenv('DROK_ENDPOINT_URL') or '').strip()


def is_configured():
    if DROK_MODE == 'hf':
        return bool(HF_TOKEN and DROK_ENDPOINT_URL)
    return True  # api & ollama need no upfront config


def _build_messages(prompt, system_prompt=None, conversation_context=None):
    """Build the messages array shared by api + ollama backends.

    conversation_context can be:
      - list of {role, content} dicts (preferred — pre-formatted history)
      - plain string (legacy; gets shoved into a system message)
      - None
    """
    messages = []
    if system_prompt:
        messages.append({'role': 'system', 'content': system_prompt})
    if conversation_context:
        if isinstance(conversation_context, list):
            messages.extend(conversation_context)
        elif isinstance(conversation_context, str) and conversation_context.strip():
            messages.append({
                'role': 'system',
                'content': 'Prior conversation:\n' + conversation_context.strip(),
            })
    messages.append({'role': 'user', 'content': prompt})
    return messages


def stream_api(prompt, system_prompt=None, conversation_context=None, timeout=None):
    """Streaming version of _ask_api — yields response chunks (cleaned strings)
    as they arrive from the standalone DROK endpoint. The standalone API
    returns the answer in chunked transfer encoding, so iterating gives the
    user word-by-word feedback instead of a 3-5s wait for the full reply.

    Usage:
        for chunk in drok.stream_api("hi"):
            yield chunk   # in a Flask streaming response
    """
    messages = _build_messages(prompt, system_prompt, conversation_context)
    payload = {'messages': messages}
    headers = {
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Origin': 'https://drok.digirocket.io',
        'Referer': 'https://drok.digirocket.io/',
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/148.0.0.0 Safari/537.36'),
    }
    try:
        with requests.post(DROK_API_URL, json=payload, headers=headers,
                           timeout=timeout or DROK_API_TIMEOUT,
                           stream=True) as r:
            r.raise_for_status()
            r.encoding = 'utf-8'
            # iter_content with small chunk_size gives word-level granularity
            # without too many tiny network reads. decode_unicode handles UTF-8.
            # NOTE: do NOT call _clean_text on streaming chunks — its .strip()
            # eats the spaces between words. Strip only ANSI + invisible /
            # control bytes here, leave whitespace alone.
            for chunk in r.iter_content(chunk_size=64, decode_unicode=True):
                if not chunk:
                    continue
                chunk = _ANSI_RE.sub('', chunk)
                chunk = _INVIS_RE.sub('', chunk)
                chunk = _CTRL_RE.sub('', chunk)
                if chunk:
                    yield chunk
    except Exception as e:
        print(f'[drok] streaming api call failed: {e}')


def _ask_api(prompt, system_prompt=None, max_tokens=400, temperature=0.7,
             conversation_context=None, timeout=None):
    """Proxy to https://drok.digirocket.io/api/chat — gives identical answers
    to the standalone app (RAG context + same prompt template + same model)."""
    messages = _build_messages(prompt, system_prompt, conversation_context)
    # The standalone app's payload is just {"messages": [...]} — no model
    # params, temperature etc. (those are server-side).
    payload = {'messages': messages}
    headers = {
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        # IMPORTANT: don't advertise br/zstd here — requests can't decode
        # Brotli/Zstd natively and the API was returning br-compressed
        # bytes that looked like garbled boxes in the chat widget. gzip
        # and deflate are handled by requests/urllib3 automatically.
        'Accept-Encoding': 'gzip, deflate',
        # Mimic a real Chrome browser session as closely as possible — the
        # standalone API's RAG layer behaves better when it thinks the
        # caller is a logged-in browser user. Without these the server
        # sometimes falls back to a stock "I don't have verified info"
        # refusal template even for queries that work in the actual app.
        'Origin': 'https://drok.digirocket.io',
        'Referer': 'https://drok.digirocket.io/',
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/148.0.0.0 Safari/537.36'),
        'Sec-Ch-Ua': '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }
    try:
        r = requests.post(DROK_API_URL, json=payload, headers=headers,
                          timeout=timeout or DROK_API_TIMEOUT)
        r.raise_for_status()
        # Response is plain text (Content-Type: text/plain), not JSON.
        # Force UTF-8 because requests sometimes guesses wrong on
        # text/plain responses (defaults to ISO-8859-1 which mangles
        # the model's accented quotes and en-dashes).
        r.encoding = 'utf-8'
        return _clean_text(r.text or '')
    except Exception as e:
        print(f'[drok] api call failed: {e}')
        return ''


def _ask_ollama(prompt, system_prompt=None, max_tokens=400, temperature=0.7,
                timeout=120, conversation_context=None):
    messages = _build_messages(prompt, system_prompt, conversation_context)
    payload = {
        'model': DROK_OLLAMA_MODEL,
        'messages': messages,
        'stream': False,
        'options': {
            'temperature': temperature,
            'num_predict': max_tokens,
        },
    }
    try:
        r = requests.post(f'{DROK_OLLAMA_HOST}/api/chat',
                          json=payload, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        return _clean_text((data.get('message') or {}).get('content') or '')
    except requests.ConnectionError:
        print('[drok] Ollama daemon not reachable at', DROK_OLLAMA_HOST)
        return ''
    except Exception as e:
        print(f'[drok] ollama call failed: {e}')
        return ''


def _ask_hf(prompt, system_prompt=None, max_tokens=400, temperature=0.7,
            timeout=60, conversation_context=None):
    messages = _build_messages(prompt, system_prompt, conversation_context)
    payload = {
        'inputs': messages,
        'parameters': {
            'max_new_tokens': max_tokens,
            'temperature': temperature,
            'return_full_text': False,
        },
    }
    headers = {'Authorization': f'Bearer {HF_TOKEN}',
               'Content-Type': 'application/json'}
    try:
        r = requests.post(DROK_ENDPOINT_URL, headers=headers, json=payload,
                          timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return _clean_text(data[0].get('generated_text') or '')
        if isinstance(data, dict):
            return _clean_text(data.get('generated_text') or '')
        return ''
    except Exception as e:
        print(f'[drok] hf call failed: {e}')
        return ''


def ask_drok(prompt, system_prompt=None, max_tokens=400, temperature=0.7,
             conversation_context=None):
    """Single entry-point — dispatches based on DROK_MODE.

    In api mode, if DROK_API_FALLBACK_OLLAMA is set and the api call returns
    an empty string (network failure, 5xx, timeout), we transparently retry
    on the local Ollama daemon. Keeps the chat working even when the
    standalone server is down.
    """
    if not is_configured():
        print('[drok] not configured — skipping')
        return ''
    if DROK_MODE == 'hf':
        return _ask_hf(prompt, system_prompt, max_tokens, temperature,
                       conversation_context=conversation_context)
    if DROK_MODE == 'ollama':
        return _ask_ollama(prompt, system_prompt, max_tokens, temperature,
                           conversation_context=conversation_context)
    # default: api mode
    reply = _ask_api(prompt, system_prompt, max_tokens, temperature,
                     conversation_context=conversation_context)
    if not reply and DROK_API_FALLBACK_OLLAMA:
        print('[drok] api returned empty — falling back to local Ollama')
        reply = _ask_ollama(prompt, system_prompt, max_tokens, temperature,
                            conversation_context=conversation_context)
    return reply


def about_digirocket_blurb(client_name=None, industry=None):
    """Generate a short 'About DigiRocket' intro for the report cover/summary."""
    parts = ['Write a concise 2-3 sentence introduction for DigiRocket Technologies']
    if client_name:
        parts.append(f'for a client named {client_name}')
    if industry:
        parts.append(f'in the {industry} industry')
    parts.append('highlighting our core services and value proposition.')
    return ask_drok(' '.join(parts) + '.', max_tokens=180)


def chat_reply(user_message, conversation_context=None):
    """Reply to a chat message.

    In api mode the standalone app already has the right system prompt /
    RAG layer baked in, so we pass system_prompt=None and let it do its
    thing. In ollama / hf mode we don't have RAG, so callers can pass a
    custom prompt if needed (but defaults to None for consistency).

    max_tokens stays at 500 — the user explicitly wants long, complete
    answers; streaming (see /api/drok-chat-stream) gives the speed-up
    without truncating content.
    """
    return ask_drok(
        user_message,
        system_prompt=None,
        max_tokens=500,
        temperature=0.3,
        conversation_context=conversation_context,
    )
