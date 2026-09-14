from app.agents.skills.dependencies import resolve_dependencies


def test_dependency_report_distinguishes_optional_and_required_missing_tools():
    report = resolve_dependencies({"tools": [
        {"name": "missing_required", "required": True},
        {"name": "plugin__demo__enrich", "required": False},
    ]}, {"read_document": {"version": "1.0.0", "provider": "client", "environment": "client"}})
    assert report.state == "unavailable"
    assert report.required_issues[0].code == "MISSING_TOOL"
    assert report.issues[1].required is False


def test_dependency_report_checks_version_and_provider():
    report = resolve_dependencies({"tools": [
        {"name": "plugin__demo__search", "min_version": "2.0.0", "provider": "desktop_mcp"},
    ]}, {"plugin__demo__search": {"version": "1.4.0", "provider": "desktop_mcp", "environment": "client"}})
    assert report.state == "unavailable"
    assert report.issues[0].code == "VERSION_MISMATCH"
