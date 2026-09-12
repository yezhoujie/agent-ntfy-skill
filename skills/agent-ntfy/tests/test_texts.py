"""固定文案的字符串表：两列 key 集合相同、英文列不含汉字、取值与语言解析。"""

import re
import unittest

import texts

HAN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]")  # 汉字 + 全角标点 / 方括号


class TableTest(unittest.TestCase):
    def test_both_columns_have_exactly_the_same_keys(self):
        self.assertEqual(texts.LANGS, ("zh", "en"))
        zh, en = set(texts.TEXTS["zh"]), set(texts.TEXTS["en"])
        self.assertEqual(zh ^ en, set(), f"只在一列里的 key：{sorted(zh ^ en)}")
        self.assertGreater(len(zh), 80)

    def test_english_column_has_no_cjk(self):
        for key, value in texts.TEXTS["en"].items():
            self.assertIsNone(HAN.search(value), f"{key}: {value!r}")

    def test_placeholders_match_between_columns(self):
        names = re.compile(r"\{(\w+)\}")
        for key in texts.TEXTS["zh"]:
            self.assertEqual(set(names.findall(texts.TEXTS["zh"][key])), set(names.findall(texts.TEXTS["en"][key])), key)

    def test_t_formats_and_rejects_unknown(self):
        self.assertEqual(texts.t("prefix.answered", "zh"), "✅ 已回复 · ")
        self.assertEqual(texts.t("prefix.answered", "en"), "✅ Answered · ")
        self.assertIn("3", texts.t("validate.header", "en", n=3))
        with self.assertRaises(KeyError):
            texts.t("no.such.key", "en")
        with self.assertRaises(KeyError):
            texts.t("prefix.answered", "jp")

    def test_literal_braces_survive_when_no_format_args(self):
        # 提示里有字面的 {id, label, consequence}：不带参数取值时不能被 format 吃掉
        self.assertIn("{id, label, consequence}", texts.t("validate.hint.options", "zh"))


class ResolveTest(unittest.TestCase):
    def test_priority_explicit_then_env_then_default(self):
        self.assertEqual(texts.resolve("zh", "en"), "zh")
        self.assertEqual(texts.resolve(None, "zh"), "zh")
        self.assertEqual(texts.resolve(None, None), "en")
        self.assertEqual(texts.DEFAULT_LANG, "en")

    def test_invalid_values_fall_through(self):
        self.assertEqual(texts.resolve("jp", "zh"), "zh")  # 非法的显式值不生效（校验层会另外报错）
        self.assertEqual(texts.resolve(None, "zh-CN"), "en")  # 只认恰好 zh / en
        self.assertEqual(texts.resolve("", "EN"), "en")
        self.assertTrue(texts.is_lang("en"))
        self.assertFalse(texts.is_lang("En"))
        self.assertFalse(texts.is_lang(None))


if __name__ == "__main__":
    unittest.main()
