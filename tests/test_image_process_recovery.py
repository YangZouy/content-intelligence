from __future__ import annotations

from src.nodes import image_process


def test_quality_repair_reuses_successful_oss_mapping(monkeypatch):
    existing_url = "https://bucket.example.com/images/article/diagram.png"

    monkeypatch.setattr(image_process, "get_oss_client", lambda: object())

    def unexpected_upload(*args, **kwargs):
        raise AssertionError("an existing OSS mapping must not be uploaded again")

    monkeypatch.setattr(image_process, "_upload_single_image", unexpected_upload)

    result = image_process.image_process_node({
        "formatted_content": "![diagram](assets/diagram.png)",
        "images": [
            {
                "url_or_path": "C:/notes/assets/diagram.png",
                "alt": "diagram",
                "usage": "inline",
            }
        ],
        "image_mapping": {"C:/notes/assets/diagram.png": existing_url},
    })

    assert result["image_mapping"] == {
        "C:/notes/assets/diagram.png": existing_url,
    }
    assert result["oss_image_count"] == 1
    assert existing_url in result["content_with_oss_images"]
