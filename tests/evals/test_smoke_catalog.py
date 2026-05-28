from __future__ import annotations

from meta_agent.evals.smoke_catalog import (
    build_payload,
    default_catalog_source,
    default_model,
    default_repo_url,
    default_verify_suite,
    render_case_summary,
    select_cases,
)


def test_default_catalog_source_points_to_remote_baseline() -> None:
    assert default_catalog_source().startswith(
        "https://raw.githubusercontent.com/gududefengzhong/meta-agent-smoke/"
    )


def test_select_cases_filters_by_batch_and_category() -> None:
    cases = [
        {
            "case": "case/a",
            "batch": "second",
            "categories": ["security", "filesystem"],
            "issue_description": "a",
            "target_files": ["a.py"],
        },
        {
            "case": "case/b",
            "batch": "third",
            "categories": ["pagination"],
            "issue_description": "b",
            "target_files": ["b.py"],
        },
    ]

    selected = select_cases(
        cases,
        case_names=[],
        batches=["second"],
        categories=["security"],
    )

    assert selected == [cases[0]]


def test_build_payload_uses_case_branch_as_base_ref() -> None:
    case = {
        "case": "case/py-safe-join-traversal",
        "issue_description": "fix traversal",
        "target_files": ["paths.py", "tests/test_paths.py"],
    }

    payload = build_payload(
        case,
        repo_url=default_repo_url(),
        verify_suite=default_verify_suite(),
        model=default_model(),
    )

    assert payload == {
        "issue_description": "fix traversal",
        "repo_url": "https://github.com/gududefengzhong/meta-agent-smoke.git",
        "base_ref": "case/py-safe-join-traversal",
        "target_files": ["paths.py", "tests/test_paths.py"],
        "verify_suite": "python_test",
        "model": "deepseek/deepseek-v4-pro",
    }


def test_render_case_summary_includes_batch_and_categories() -> None:
    summary = render_case_summary(
        {
            "case": "case/py-safe-join-traversal",
            "batch": "second",
            "categories": ["security", "path-traversal"],
        }
    )

    assert summary == (
        "case/py-safe-join-traversal | batch=second | "
        "categories=security, path-traversal"
    )
