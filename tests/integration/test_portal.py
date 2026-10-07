"""The portal: one address (http://localhost:8000) for every tool, by path, without touching the app's nginx."""

import httpx
import pytest

from conftest import PORTAL_URL, loki_streams


@pytest.fixture(scope="module")
def portal():
    with httpx.Client(base_url=PORTAL_URL, timeout=10) as client:
        yield client


@pytest.mark.parametrize("path", [
    "/", "/grafana/api/health", "/grafana/", "/prometheus/-/ready", "/prometheus/api/v1/query?query=up",
    "/faults/health", "/faults/faults?state=active", "/experiments/health", "/experiments/experiments",
    "/app/nginx-health", "/app/health",
])
def test_every_tool_by_path(portal, path):
    assert portal.get(path).status_code == 200


@pytest.mark.parametrize("prefix", ["/faults", "/experiments", "/app"])
def test_swagger_works_under_the_prefix(portal, prefix):
    """The prefix is stripped by the portal and comes back as root_path, so Swagger finds its document."""
    assert f"url: '{prefix}/openapi.json'" in portal.get(f"{prefix}/docs").text
    assert portal.get(f"{prefix}/openapi.json").json()["servers"] == [{"url": prefix}]


def test_grafana_and_prometheus_links_carry_the_prefix(portal):
    assert '<base href="/grafana/"' in portal.get("/grafana/").text
    redirect = portal.get("/prometheus/", follow_redirects=False)
    assert redirect.headers["location"].endswith("/prometheus/query")


def test_portal_invisible_to_diagnostic_plane(portal):
    portal.get("/faults/faults")
    assert loki_streams('{service="portal"}', since="1h") == []


# ---------------------------------------------------------------- Control Center (the single-page app at /)

UI_FILES = ["/ui/app.css", "/ui/js/main.js", "/ui/js/api.js", "/ui/js/ui.js", "/ui/js/overview.js", "/ui/js/faults.js",
            "/ui/js/experiments.js", "/ui/js/logs.js", "/ui/vendor/preact-htm.js", "/ui/vendor/uPlot.esm.js",
            "/ui/vendor/uPlot.min.css"]


def test_control_center_is_served(portal):
    page = portal.get("/").text
    assert 'id="root"' in page and "/ui/js/main.js" in page
    for path in UI_FILES:
        assert portal.get(path).status_code == 200, path


def test_control_center_works_offline(portal):
    """Libraries are vendored: no CDN or other external URL is loaded (the testbed may run without internet)."""
    for path in ["/", *UI_FILES]:
        if "vendor" in path:
            continue
        body = portal.get(path).text
        assert "https://" not in body and "http://" not in body.replace("http://www.w3.org", ""), path


def test_fault_catalog_for_forms(faults):
    catalog = {t["type"]: t for t in faults.get("/fault-types").json()}
    assert len(catalog) == 22
    latency = catalog["payment_latency"]
    assert latency["targets"] == ["payment"]
    assert latency["parameters"]["properties"]["latency_ms"]["default"] == 2000
    assert "delay_ms" in catalog["network_latency"]["parameters"]["required"]
    assert "DB_POOL_MAX_SIZE" in catalog["bad_configuration"]["per_target"]["order"]
    assert catalog["incorrect_endpoint"]["per_target"]["order"] == ["payment", "postgres"]
