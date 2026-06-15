import json
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты — помощник для подсчёта калорий. Пользователь описывает, что он съел, на русском языке.

Твоя задача — извлечь из текста все продукты и их количество, и оценить их калорийность и БЖУ (белки, жиры, углеводы).

ВАЖНЫЕ ПРАВИЛА:
1. Если пользователь пишет про миску/тарелку и вес вместе:
   - "миска 350г, вес 680г" → вес нетто = 680 - 350 = 330г
   - "тарелка 400г" и в следующей строке "вес 950г" — тоже вычти
   - Игнорируй вес посуды, считай только вес еды
2. Если вес указан явно — ВСЕГДА используй его. weight_type = "exact".
   Если вес не указан — оцени стандартную порцию (weight_type="estimated"):
   - Супы/борщи/каши на воде — ~250-300г
   - Гарниры (гречка, рис, макароны) — ~150-200г
   - Мясо/рыба/птица — ~150г, если не указано иное
   - Масло/соусы — ~15-20г
   - Овощи в салате — ~100-150г
   - Яйцо — 1 шт (~60г)
   - Хлеб — 1 кусок (~30г)
3. КРУПЫ: Если пользователь пишет «рис», «гречка», «макароны», «паста», «овсянка», «киноа» и т.п. без уточнения «сухой»/«сырая» — считай что это ВАРЁНЫЙ/ГОТОВЫЙ продукт. КБЖУ варёных круп сильно ниже сырых (рис варёный ~130 ккал/100г, гречка варёная ~110 ккал/100г, макароны варёные ~130 ккал/100г).
4. Если пользователь указал БЖУ явно (например "7.3 жира", "30г белка"), ИСПОЛЬЗУЙ эти значения.
5. КРИТИЧНО: Если пользователь написал "Xг продукта (Y ккал)", то Y — это ОБЩАЯ калорийность для X граммов, а не на 100г. Посчитай КБЖУ на 100г пропорционально: на 100г = Y * 100 / X. Но в ответе пиши weight_g = X, а kcal = Y (общая).
6. Если в сообщении нет еды (приветствие, вопрос и т.п.) — верни пустой список items.
7. Возвращай ТОЛЬКО JSON. Никакого другого текста, пояснений, рассуждений.

Формат ответа:
{
  "items": [
    {
      "name": "название продукта на русском языке",
      "weight_g": число граммов,
      "weight_type": "exact" или "estimated",
      "kcal": число,
      "protein_g": число,
      "fat_g": число,
      "carbs_g": число
    }
  ]
}"""

IMPORT_SYSTEM_PROMPT = """Ты — ассистент для импорта базы продуктов пользователя.

Пользователь присылает список своих продуктов с их калорийностью и БЖУ.
Твоя задача — извлечь из текста каждый продукт и вернуть его в структурированном JSON.

ВАЖНЫЕ ПРАВИЛА:
1. Если указан вес (например "на 100г: X ккал" или "150г: Y ккал"), используй его как weight_g
2. Если вес не указан — пиши weight_g = 100, а weight_type = "estimated"
3. Бери значения КБЖУ ТОЛЬКО из текста, не додумывай
4. Каждый продукт — отдельный элемент в items
5. Возвращай ТОЛЬКО JSON. Никакого другого текста.

Формат ответа:
{
  "items": [
    {
      "name": "название продукта",
      "weight_g": число,
      "weight_type": "exact" или "estimated",
      "kcal": число,
      "protein_g": число,
      "fat_g": число,
      "carbs_g": число
    }
  ]
}"""


def _extract_json(text: str) -> str:
    """Извлечение JSON из ответа модели (может быть обёрнут в markdown)."""
    text = text.strip()

    if text.startswith("```"):
        end = text.rfind("```")
        if end > 3:
            text = text[3:end].strip()
        if text.startswith("json"):
            text = text[4:].strip()
        elif text.startswith("JSON"):
            text = text[4:].strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]

    return text


def _normalise_items(items: list[dict]) -> list[dict]:
    """Привести поля к единому типу."""
    for item in items:
        item["name"] = item.get("name", "?") or "?"
        item["weight_g"] = float(item.get("weight_g", 0) or 0)
        item["weight_type"] = item.get("weight_type", "estimated") or "estimated"
        item["kcal"] = float(item.get("kcal", 0) or 0)
        item["protein_g"] = float(item.get("protein_g", 0) or 0)
        item["fat_g"] = float(item.get("fat_g", 0) or 0)
        item["carbs_g"] = float(item.get("carbs_g", 0) or 0)
    return items


class LLMParser:
    """Парсинг описания еды через OpenRouter LLM."""

    def __init__(
        self,
        api_key: str = settings.openrouter_api_key,
        model: str = settings.model,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1"

    async def parse(
        self,
        text: str,
        context: str | None = None,
        system: str | None = None,
        max_tokens: int = 2000,
        timeout: int = 30,
        raw: bool = False,
    ) -> list[dict] | dict:
        """Разобрать текст с описанием еды, вернуть список продуктов.

        context — проверенные КБЖУ пользователя (вставляется как приоритет).
        system  — кастомный system prompt (если None, используется SYSTEM_PROMPT).
        max_tokens — макс. токенов в ответе.
        timeout  — таймаут на HTTP запрос в секундах.
        raw     — если True, возвращает полный JSON-ответ (не только items).
        """
        logger.info("Parsing: %s", text[:80])

        sys_prompt = system if system else SYSTEM_PROMPT
        messages = [{"role": "system", "content": sys_prompt}]
        if context:
            messages.append({
                "role": "system",
                "content": (
                    "НИЖЕ — проверенные данные пользователя о его продуктах.\n"
                    "Используй их В ПЕРВУЮ ОЧЕРЕДЬ. Если продукт есть в этом списке, "
                    "всегда бери КБЖУ оттуда, а не из общих таблиц.\n\n"
                    f"{context}"
                ),
            })
        messages.append({"role": "user", "content": text})

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/nousresearch/hermes",
                },
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]

        json_str = _extract_json(content)
        result = json.loads(json_str)

        if raw:
            return result

        items = result.get("items", [])
        if not isinstance(items, list):
            items = []

        return _normalise_items(items)