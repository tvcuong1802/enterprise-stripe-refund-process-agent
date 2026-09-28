"""Two-way tests for the language detector -- mechanism only, no per-agent tuning."""

from src.services.disclaimer import language_of_answer as f

JA_REPORT = (
    "# METI v1.2 HITL コンプライアンス・ギャップ報告書 エージェント agent-x1 "
    "総合判定 不適合 システム分類 フィジカル AI 要件別評価 ギャップあり 最重要 "
    "ハードウェア安全オーバーライドの確認が必要です 参照条項 人間による関与の実装"
)
EN_REPORT = (
    "# METI v1.2 Compliance Gap Report Overall Verdict FAIL System Classification "
    "Physical AI external actions detected Requirement Assessment gap status critical"
)
BILINGUAL = (
    "No matching documentation was found for this query. Please paste the log lines "
    "themselves and try again shortly. この質問に一致するドキュメントは見つかりませんでした。"
    "ログ行そのものを貼り付けて、しばらくしてから再度お試しください。"
)


def test_a_translated_japanese_report_is_japanese_despite_latin_names():
    assert f(JA_REPORT) == "ja"


def test_an_english_report_is_english():
    assert f(EN_REPORT) == "en"


def test_a_message_written_in_both_languages_is_both():
    assert f(BILINGUAL) is None


def test_short_or_empty_answers_are_both_so_the_notice_is_bilingual():
    assert f("") is None and f("OK") is None and f("はい") is None


def test_policy_is_per_agent_not_baked_in():
    # An agent that wants every mixed answer bilingual disables dominance...
    assert f(JA_REPORT, policy={"dominance": 0}) == "ja"  # names still are not prose
    # ...and one that keeps a mixed-case product name in Latin declares it.
    mixed = JA_REPORT.replace("フィジカル AI", "Physical Artificial Intelligence 分類")
    assert f(mixed, policy={"identifiers": ("Physical Artificial Intelligence",)}) == "ja"


def test_raising_the_floor_suppresses_a_language_that_barely_appears():
    assert f(BILINGUAL, policy={"min_chars": 500}) is None


# The load-bearing proof that the name filter matters is a mutation test, run from
# tooling: delete the `not w.isupper() and ... not in names` guard and the Japanese
# report test must go red. Asserting it from inside the suite only restates the fixture.

JA_WITH_LOGS = (
    "障害の再構成です。以下のログ行から、決済ゲートウェイのタイムアウトが先行し、"
    "その後に再試行が集中したことが読み取れます。データベースログと照合してください。\n"
    "```\n2026-08-28T06:01 gateway upstream timeout retry backoff exhausted "
    "connection pool saturated downstream payment service unavailable\n```"
)


def test_quoted_machine_output_does_not_make_a_japanese_answer_bilingual():
    assert f(JA_WITH_LOGS) == "ja"


def test_an_agent_whose_fences_hold_prose_can_switch_that_off():
    assert f(JA_WITH_LOGS, policy={"ignore_quoted_code": False}) is None


def test_a_malformed_policy_falls_back_instead_of_raising_mid_answer():
    ja = "これは日本語の回答です。十分な長さがあります。参考情報としてご覧ください。"
    assert f(ja, policy={"min_chars": "many", "identifiers": 42, "dominance": None}) == "ja"


def test_an_agent_can_declare_its_own_machine_markup():
    # Measured on (internal reference removed): a Japanese answer carrying "[endpoint: /k/v1/records GET]"
    # markers had 53 Japanese characters against 52 of Latin, all of it marker text.
    answer = (
        "kintone アプリから複数のレコードを取得するには `GET /k/v1/records` を使います。"
        "[endpoint: /k/v1/records GET] 詳細は公式ドキュメントをご確認ください。"
        "[endpoint: /k/v1/records GET]"
    )
    # Explicitly empty, not omitted: a repo that declares its own patterns in
    # agent_scope.py would otherwise have them applied here and the contrast would vanish.
    assert f(answer, policy={"ignore_patterns": ()}) is None, "undeclared, markers read as English"
    assert f(answer, policy={"ignore_patterns": (r"\[endpoint:[^\]]*\]",)}) == "ja"


def test_a_broken_regex_in_a_policy_does_not_break_the_answer():
    ja = "これは日本語の回答です。十分な長さがあります。参考情報としてご覧ください。"
    assert f(ja, policy={"ignore_patterns": ("[unclosed",)}) == "ja"


def test_a_marker_inside_a_code_fence_is_data_not_a_notice():
    """Measured on (internal reference removed), 2026-08-28.

    Its answer ends with a machine-readable JSON block carrying
    `"disclaimer": "AI-generated. Human review required before publication."`. A plain
    substring search found that, concluded a trailer was already present, and skipped
    appending one -- so the reader got a JSON field where the liability notice should
    have been, and every check agreed because the string really was there.
    """
    from src.services.disclaimer import has_disclaimer, is_bilingual

    answer = (
        "台本の本文です。ここに本文が入ります。\n\n"
        '```json\n{"disclaimer": "AI-generated. Human review required before publication."}\n```'
    )
    assert not has_disclaimer(answer), "a marker inside a fence must not count as a trailer"
    assert not is_bilingual(answer)

    with_notice = answer + "\n\n---\n\n_AI-generated draft. Human review required._"
    assert has_disclaimer(with_notice)
