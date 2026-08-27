import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = Path(__file__).resolve().parents[1]
COVERAGE_DIR = REPO_ROOT / "servers" / "flutter_test_coverage"
SECURITY_DIR = REPO_ROOT / "servers" / "mobile_security"

LCOV_CONTENTS = """\
SF:lib/main.dart
DA:1,1
DA:2,0
LF:2
LH:1
end_of_record
"""

DART_WITH_FAKE_SECRET = """\
const awsKey = 'AKIAABCDEFGHIJKLMNOP';
"""

TOML_TEMPLATE = """
[servers.flutestcov]
description = "Analyzes Flutter's lcov coverage report"
command = "python"
args = ["-m", "flutter_test_coverage.server"]
cwd = "{coverage_dir}"

[servers.mobilesec]
description = "Static security scan of a Flutter/Android/iOS project"
command = "python"
args = ["-m", "mobile_security.server"]
cwd = "{security_dir}"

[servers.gateway]
description = "The gateway's own entry -- must not be treated as a backend"
command = "python"
args = ["-m", "mcp_gateway.server"]
cwd = "{gateway_dir}"
"""


@pytest.fixture(name="flutter_project")
def flutter_project_fixture(tmp_path):
    project = tmp_path / "flutter_project"
    lib_dir = project / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "main.dart").write_text(DART_WITH_FAKE_SECRET)
    (project / "coverage").mkdir()
    (project / "coverage" / "lcov.info").write_text(LCOV_CONTENTS)
    return project


@pytest.fixture(name="gateway_config")
def gateway_config_fixture(tmp_path):
    config_file = tmp_path / "servers.toml"
    config_file.write_text(
        TOML_TEMPLATE.format(
            coverage_dir=COVERAGE_DIR, security_dir=SECURITY_DIR, gateway_dir=GATEWAY_DIR
        )
    )
    return config_file


async def _run_session(gateway_config, fn):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_gateway.server"],
        cwd=GATEWAY_DIR,
        env={"MCP_GATEWAY_CONFIG_PATH": str(gateway_config)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


async def test_list_tools_aggregates_both_backends_under_namespaced_names(gateway_config):
    async def scenario(session: ClientSession):
        result = await session.list_tools()
        return {t.name for t in result.tools}

    names = await _run_session(gateway_config, scenario)

    assert "flutestcov__parse_coverage_report" in names
    assert "mobilesec__scan_for_secrets" in names
    # the gateway's own servers.toml entry must never appear as a pseudo-backend
    assert not any(n.startswith("gateway__") for n in names)


async def test_call_tool_routes_to_the_test_coverage_backend(gateway_config, flutter_project):
    async def scenario(session: ClientSession):
        return await session.call_tool(
            "flutestcov__parse_coverage_report", {"project_path": str(flutter_project)}
        )

    result = await _run_session(gateway_config, scenario)

    assert not result.is_error
    assert "overall_line_coverage_percent" in result.content[0].text


async def test_call_tool_routes_to_the_mobile_security_backend(gateway_config, flutter_project):
    async def scenario(session: ClientSession):
        return await session.call_tool(
            "mobilesec__scan_for_secrets", {"project_path": str(flutter_project)}
        )

    result = await _run_session(gateway_config, scenario)

    assert not result.is_error
    assert "AKIA" in result.content[0].text


async def test_call_tool_returns_a_clear_error_for_an_unknown_backend(gateway_config):
    async def scenario(session: ClientSession):
        return await session.call_tool("nonexistent__some_tool", {})

    result = await _run_session(gateway_config, scenario)

    assert result.is_error
    assert "Unknown gateway backend 'nonexistent'" in result.content[0].text
