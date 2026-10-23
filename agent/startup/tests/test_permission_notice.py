import pytest

from agent.startup.permission_notice import PermissionNotice, permission_notice


def test_one_fault_keeps_its_own_headline_and_diagnostic() -> None:
    assert permission_notice([("Screen Recording off", "Grant it in System Settings.")]) == (
        PermissionNotice(title="Screen Recording off", content="Grant it in System Settings.")
    )


def test_several_faults_share_one_notice_that_names_each() -> None:
    assert permission_notice(
        [("Screen Recording off", "Grant screen."), ("Accessibility off", "Grant accessibility.")]
    ) == PermissionNotice(
        title="Desktop permissions missing",
        content="Screen Recording off: Grant screen.\n\nAccessibility off: Grant accessibility.",
    )


def test_a_notice_without_a_fault_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one fault"):
        permission_notice([])
