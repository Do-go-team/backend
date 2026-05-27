from django.test import SimpleTestCase

from common.sanitize import MASK, sanitize


class SanitizeTests(SimpleTestCase):
    """PII 마스킹 — Sentry 전송 직전 거치는 필터. 키 정확 매칭만."""

    def test_masks_password_in_flat_dict(self):
        result = sanitize({"email": "a@b.c", "password": "secret123"})
        self.assertEqual(result, {"email": "a@b.c", "password": MASK})

    def test_masks_case_insensitive(self):
        result = sanitize({"Password": "x", "API_KEY": "y", "AUTHORIZATION": "z"})
        self.assertEqual(result["Password"], MASK)
        self.assertEqual(result["API_KEY"], MASK)
        self.assertEqual(result["AUTHORIZATION"], MASK)

    def test_masks_recursively_in_nested_dict(self):
        result = sanitize({"user": {"email": "a@b.c", "token": "abc"}})
        self.assertEqual(result, {"user": {"email": "a@b.c", "token": MASK}})

    def test_masks_inside_list_of_dicts(self):
        result = sanitize([{"password": "x"}, {"refresh_token": "y"}])
        self.assertEqual(result, [{"password": MASK}, {"refresh_token": MASK}])

    def test_passes_through_non_sensitive_keys(self):
        # user_id, token_count 같은 키는 SENSITIVE_KEYS 정확 매칭이 아니라 유지.
        result = sanitize({"user_id": 7, "token_count": 3})
        self.assertEqual(result, {"user_id": 7, "token_count": 3})

    def test_does_not_mutate_input(self):
        original = {"password": "x", "nested": {"token": "y"}}
        sanitize(original)
        self.assertEqual(original, {"password": "x", "nested": {"token": "y"}})

    def test_scalar_passthrough(self):
        self.assertEqual(sanitize("plain"), "plain")
        self.assertEqual(sanitize(42), 42)
        self.assertIsNone(sanitize(None))
