import pytest
from llm_parser import _extract_json, _normalise_items


class TestExtractJson:
    def test_plain_json(self):
        text = '{"items": []}'
        assert _extract_json(text) == '{"items": []}'

    def test_markdown_json_block(self):
        text = '```json\n{"items": []}\n```'
        result = _extract_json(text)
        assert result.strip().startswith("{")

    def test_markdown_no_lang(self):
        text = '```\n{"items": []}\n```'
        result = _extract_json(text)
        assert result.strip().startswith("{")

    def test_json_uppercase_markdown(self):
        text = '```JSON\n{"items": []}\n```'
        result = _extract_json(text)
        assert result.strip().startswith("{")

    def test_json_with_leading_text(self):
        text = 'Sure! Here it is: {"items": [{"name": "test"}]}'
        result = _extract_json(text)
        assert result.startswith("{")
        assert '"items"' in result

    def test_extracts_only_json_object(self):
        text = 'noise before {"key": "val"} noise after'
        result = _extract_json(text)
        assert result == '{"key": "val"}'


class TestNormaliseItems:
    def test_coerces_string_numbers(self):
        items = [{"name": "egg", "weight_g": "60", "weight_type": "exact",
                  "kcal": "85", "protein_g": "7.0", "fat_g": "5.9", "carbs_g": "0.6"}]
        result = _normalise_items(items)
        assert isinstance(result[0]["weight_g"], float)
        assert isinstance(result[0]["kcal"], float)
        assert result[0]["kcal"] == pytest.approx(85.0)

    def test_default_weight_type_when_none(self):
        items = [{"name": "x", "weight_g": 100, "weight_type": None,
                  "kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 10}]
        result = _normalise_items(items)
        assert result[0]["weight_type"] == "estimated"

    def test_default_name_when_missing(self):
        items = [{"weight_g": 100, "weight_type": "exact",
                  "kcal": 100, "protein_g": 5, "fat_g": 2, "carbs_g": 10}]
        result = _normalise_items(items)
        assert result[0]["name"] == "?"

    def test_none_kcal_becomes_zero(self):
        items = [{"name": "x", "weight_g": 100, "weight_type": "exact",
                  "kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None}]
        result = _normalise_items(items)
        assert result[0]["kcal"] == 0.0
        assert result[0]["protein_g"] == 0.0
