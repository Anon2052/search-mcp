"""Demo walkthrough video route: serves the ChatGPT app review recording
(plugin manifest review.demo_recording_url) publicly and free."""

from __future__ import annotations

from fastapi.testclient import TestClient

from search_mcp.rest_app import create_app


def test_demo_video_404_when_file_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_VIDEO_PATH", str(tmp_path / "missing.mp4"))
    with TestClient(create_app()) as client:
        r = client.get("/demo-walkthrough.mp4")
    assert r.status_code == 404


def test_demo_video_served_when_present(monkeypatch, tmp_path):
    video = tmp_path / "demo.mp4"
    payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 1024
    video.write_bytes(payload)
    monkeypatch.setenv("DEMO_VIDEO_PATH", str(video))
    with TestClient(create_app()) as client:
        r = client.get("/demo-walkthrough.mp4")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("video/mp4")
        assert r.content == payload
        # Range requests work (browser/review-player seeking).
        r2 = client.get("/demo-walkthrough.mp4", headers={"Range": "bytes=0-99"})
        assert r2.status_code == 206
        assert len(r2.content) == 100
