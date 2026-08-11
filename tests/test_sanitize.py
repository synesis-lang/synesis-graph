"""Tests for sanitize.py — pure functions, no external dependencies."""

from __future__ import annotations

import pytest

from synesis_graph.sanitize import (
    sanitize_cypher_label,
    sanitize_database_name,
    validate_cypher_label,
)

# ---------------------------------------------------------------------------
# sanitize_cypher_label
# ---------------------------------------------------------------------------


class TestSanitizeCypherLabel:
    def test_clean_label_unchanged(self):
        assert sanitize_cypher_label("SocialCohesion") == "SocialCohesion"

    def test_underscores_kept(self):
        assert sanitize_cypher_label("Social_Cohesion") == "Social_Cohesion"

    def test_spaces_removed(self):
        assert sanitize_cypher_label("Social Cohesion") == "SocialCohesion"

    def test_hyphens_removed(self):
        assert sanitize_cypher_label("Social-Cohesion") == "SocialCohesion"

    def test_special_chars_removed(self):
        assert sanitize_cypher_label("A@B#C!") == "ABC"

    def test_leading_digit_gets_prefix(self):
        result = sanitize_cypher_label("123concept")
        assert result.startswith("_")
        assert "123concept" in result

    def test_empty_string_returns_unknown(self):
        assert sanitize_cypher_label("") == "Unknown"

    def test_all_special_chars_returns_unknown(self):
        assert sanitize_cypher_label("@#$%") == "Unknown"

    def test_unicode_letters_kept(self):
        result = sanitize_cypher_label("Résilience")
        assert "R" in result
        assert "silience" in result

    def test_numbers_in_middle_kept(self):
        assert sanitize_cypher_label("concept2025") == "concept2025"

    def test_only_digit_returns_prefixed(self):
        result = sanitize_cypher_label("9")
        assert result == "_9"


# ---------------------------------------------------------------------------
# sanitize_database_name
# ---------------------------------------------------------------------------


class TestSanitizeDatabaseName:
    def test_clean_name_lowercase(self):
        assert sanitize_database_name("MyProject") == "myproject"

    def test_underscores_converted_to_hyphens(self):
        assert sanitize_database_name("my_project") == "my-project"

    def test_spaces_removed(self):
        assert sanitize_database_name("my project") == "myproject"

    def test_dots_kept(self):
        assert sanitize_database_name("my.project") == "my.project"

    def test_hyphens_kept(self):
        assert sanitize_database_name("my-project") == "my-project"

    def test_leading_digit_gets_db_prefix(self):
        result = sanitize_database_name("123project")
        assert result.startswith("db")

    def test_leading_hyphen_gets_db_prefix(self):
        result = sanitize_database_name("-project")
        assert result.startswith("db")

    def test_empty_string_returns_synesis(self):
        assert sanitize_database_name("") == "synesis"

    def test_all_invalid_chars_returns_synesis(self):
        assert sanitize_database_name("@#$!") == "synesis"

    def test_mixed_case_lowercased(self):
        assert sanitize_database_name("DaviPesquisa") == "davipesquisa"

    def test_special_chars_stripped(self):
        assert sanitize_database_name("project@2025!") == "project2025"

    def test_unicode_stripped(self):
        result = sanitize_database_name("Résumé")
        assert result == "synesis" or result.isalnum()

    @pytest.mark.parametrize("name,expected", [
        ("face85", "face85"),
        ("FACE_UFMG", "face-ufmg"),
        ("Davi_Pesquisa_2024", "davi-pesquisa-2024"),
        ("lattes.export", "lattes.export"),
    ])
    def test_real_project_names(self, name: str, expected: str):
        assert sanitize_database_name(name) == expected


# ---------------------------------------------------------------------------
# validate_cypher_label
# ---------------------------------------------------------------------------


class TestValidateCypherLabel:
    def test_valid_label_returns_true(self):
        assert validate_cypher_label("SocialCohesion") is True

    def test_label_with_underscore_returns_true(self):
        assert validate_cypher_label("Social_Cohesion") is True

    def test_label_starting_with_underscore_returns_true(self):
        assert validate_cypher_label("_concept") is True

    def test_label_with_numbers_returns_true(self):
        assert validate_cypher_label("concept2025") is True

    def test_label_starting_with_digit_returns_false(self):
        assert validate_cypher_label("2concept") is False

    def test_label_with_space_returns_false(self):
        assert validate_cypher_label("Social Cohesion") is False

    def test_label_with_hyphen_returns_false(self):
        assert validate_cypher_label("Social-Cohesion") is False

    def test_empty_string_returns_false(self):
        assert validate_cypher_label("") is False

    def test_special_chars_return_false(self):
        assert validate_cypher_label("A@B") is False
