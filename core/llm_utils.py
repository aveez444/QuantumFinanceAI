# llm_utils.py
import logging
import json
from typing import Any, Dict, Optional
from django.conf import settings

logger = logging.getLogger(__name__)

def _build_messages(prompt: str, system_msg: str) -> list[dict]:
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt}
    ]

def _parse_json_or_error(content: str) -> Any:
    try:
        return json.loads(content)
    except Exception:
        logger.warning("Invalid JSON returned from LLM: %s", content[:400])
        return {"error": "invalid_json", "raw": content}

def call_llm(prompt: str, response_format: str = "json_object") -> Any:
    """
    Robust LLM caller.
    Strategy:
      1. If a GROQ_API_KEY is configured, try to use groq SDK (lazy import).
      2. If groq is missing / fails or GROQ not configured, fallback to OpenAI if OPENAI_API_KEY is configured.
      3. Return dict for json_object, or string for text. On error return {'error': '...'}.
    """
    groq_key = settings.AI_SETTINGS.get("GROQ_API_KEY") if getattr(settings, "AI_SETTINGS", None) else None
    openai_key = settings.AI_SETTINGS.get("OPENAI_API_KEY") if getattr(settings, "AI_SETTINGS", None) else None

    # system message for json/text
    if response_format == "json_object":
        system_msg = "You are a manufacturing ERP assistant. Respond in valid JSON only."
    else:
        system_msg = "You are a manufacturing ERP assistant. Answer concisely in plain text (1-2 sentences)."

    messages = _build_messages(prompt, system_msg)
    model = settings.AI_SETTINGS.get("DEFAULT_MODEL", "gpt-4o-mini") if getattr(settings, "AI_SETTINGS", None) else "gpt-4o-mini"
    max_tokens = settings.AI_SETTINGS.get("MAX_TOKENS", 512) if getattr(settings, "AI_SETTINGS", None) else 512
    temperature = settings.AI_SETTINGS.get("TEMPERATURE", 0.3) if getattr(settings, "AI_SETTINGS", None) else 0.3

    # ------------- Try Groq first (if configured) -------------
    if groq_key:
        try:
            # lazy import so missing package doesn't break startup
            from groq import Groq  # may raise ImportError or AttributeError
            client = Groq(api_key=groq_key)

            params = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if response_format == "json_object":
                params["response_format"] = {"type": "json_object"}

            completion = client.chat.completions.create(**params)
            response_content = completion.choices[0].message.content

            if response_format == "json_object":
                return _parse_json_or_error(response_content)
            return response_content.strip()
        except Exception as e:
            logger.warning("Groq client failed or unavailable: %s — falling back to OpenAI if available", str(e))

    # ------------- Fallback to OpenAI -------------
    if openai_key:
        try:
            import openai
            openai.api_key = openai_key

            # Use ChatCompletion API (works with many openai client versions)
            chat_messages = [{"role": m["role"], "content": m["content"]} for m in messages]

            resp = openai.ChatCompletion.create(
                model=model,
                messages=chat_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            # Content extraction compatible with both 'message' and text fields
            choice = resp.choices[0]
            # Try nested message.content first (chat format), fallback to text
            response_content = None
            if hasattr(choice, "message") and isinstance(choice.message, dict) and choice.message.get("content"):
                response_content = choice.message.get("content")
            elif choice.get("message") and choice["message"].get("content"):
                response_content = choice["message"]["content"]
            else:
                # fallback to 'text' (older responses)
                response_content = choice.get("text") or ""

            if response_format == "json_object":
                return _parse_json_or_error(response_content)
            return response_content.strip()
        except Exception as e:
            logger.error("OpenAI LLM call failed: %s", str(e))
            return {"error": f"LLM error: {str(e)}"}

    # ------------- No provider configured -------------
    logger.error("No LLM provider configured: set GROQ_API_KEY or OPENAI_API_KEY in AI_SETTINGS.")
    return {"error": "no_llm_provider_configured"}
