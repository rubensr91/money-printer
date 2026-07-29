import os
from openai import OpenAI

from config import get_deepseek_api_key, get_deepseek_base_url, get_deepseek_model

_selected_model: str | None = None


def _client() -> OpenAI:
    return OpenAI(
        api_key=get_deepseek_api_key(),
        base_url=get_deepseek_base_url(),
    )


def select_model(model: str) -> None:
    global _selected_model
    _selected_model = model


def get_active_model() -> str | None:
    return _selected_model


def generate_text(prompt: str, model_name: str = None) -> str:
    model = model_name or _selected_model
    if not model:
        model = get_deepseek_model()
    if not model:
        raise RuntimeError(
            "No model selected. Call select_model() first or pass model_name."
        )

    response = _client().chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"thinking": {"type": "disabled"}},
    )

    return response.choices[0].message.content.strip()
