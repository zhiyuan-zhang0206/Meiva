from schedules.plans.watch.junit_cases import CaseOutcome, CaseResult, collected_case_results


def test_collected_result_distinguishes_pass_skip_and_red() -> None:
    xml = """<testsuite>
      <testcase file="tests/a.py" name="pass"/>
      <testcase file="tests/a.py" name="skip"><skipped/></testcase>
      <testcase file="tests/a.py" name="fail"><failure/></testcase>
      <testcase file="tests/a.py" name="error"><error/></testcase>
    </testsuite>"""
    assert collected_case_results(xml) == (
        CaseResult("tests/a.py::pass", CaseOutcome.PASSED),
        CaseResult("tests/a.py::skip", CaseOutcome.SKIPPED),
        CaseResult("tests/a.py::fail", CaseOutcome.FAILED),
        CaseResult("tests/a.py::error", CaseOutcome.FAILED),
    )


def test_unknown_document_cannot_claim_a_case_was_not_collected() -> None:
    assert collected_case_results("<truncated") is None
    assert collected_case_results("<html/>") is None
    assert collected_case_results('<testsuite><testcase name="anonymous"/></testsuite>') is None
    assert collected_case_results("<testsuite/>") == ()


def test_classname_only_pytest_module_uses_the_same_id_as_file_metadata() -> None:
    one = collected_case_results(
        '<testsuite><testcase classname="tests.e2e.visual.test_layout_invariants" name="test_bottom_rule_gap_is_halved[390-844]"/></testsuite>'
    )
    two = collected_case_results(
        '<testsuite><testcase file="tests/e2e/visual/test_layout_invariants.py" name="test_bottom_rule_gap_is_halved[390-844]"/></testsuite>'
    )
    assert one == two
    unknown = collected_case_results(
        '<testsuite><testcase classname="custom.ExampleClass" name="test_a"/></testsuite>'
    )
    assert unknown is not None
    assert unknown[0].test_id == "custom.ExampleClass::test_a"
