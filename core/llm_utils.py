# llm_utils.py
import logging
import json
from groq import Groq
from django.conf import settings

logger = logging.getLogger(__name__)

def call_llm(prompt, response_format="json_object"):
    """
    Call Groq LLM API.
    - response_format: "json_object" (default) or "text"
    - Returns JSON (dict) or plain string depending on mode.
    """
    client = Groq(api_key=settings.AI_SETTINGS['GROQ_API_KEY'])

    if response_format == "json_object":
        system_msg = "You are a manufacturing ERP assistant. Respond in valid JSON only."
    else:
        system_msg = "You are a manufacturing ERP assistant. Answer concisely in plain text (1-2 sentences)."

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt}
    ]

    params = {
        "model": "llama-3.1-8b-instant",
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.3,
    }

    if response_format == "json_object":
        params["response_format"] = {"type": "json_object"}

    try:
        completion = client.chat.completions.create(**params)
        response_content = completion.choices[0].message.content

        if response_format == "json_object":
            try:
                return json.loads(response_content)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON returned from LLM.")
                return {"error": "invalid_json", "raw": response_content}
        else:
            return response_content.strip()

    except Exception as e:
        logger.error(f"LLM API error: {str(e)}")
        return {"error": f"LLM error: {str(e)}"}
