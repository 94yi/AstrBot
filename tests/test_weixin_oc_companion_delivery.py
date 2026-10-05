import base64
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import (
    WeixinOCAdapter,
)

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
CLIENT_ID = "a" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42"


def test_inbound_media_text_exposes_only_bounded_semantics():
    adapter = object.__new__(WeixinOCAdapter)
    items = [
        {"type": 1, "text_item": {"text": "看看"}},
        {
            "type": adapter.IMAGE_ITEM_TYPE,
            "image_item": {"media": {"encrypt_query_param": "private-image-url"}},
        },
        {
            "type": adapter.VOICE_ITEM_TYPE,
            "voice_item": {
                "text": "  明天\n九点见  ",
                "media": {"encrypt_query_param": "private-voice-url"},
            },
        },
        {
            "type": adapter.FILE_ITEM_TYPE,
            "file_item": {
                "file_name": "../课程表.pdf",
                "media": {"encrypt_query_param": "private-file-url"},
            },
        },
        {
            "type": adapter.VIDEO_ITEM_TYPE,
            "video_item": {"media": {"encrypt_query_param": "private-video-url"}},
        },
    ]

    text = adapter._message_text_from_item_list(items)

    assert text == "看看\n[图片]\n[语音转写：明天 九点见]\n[文件：课程表.pdf]\n[视频]"
    assert "private-" not in text
    assert "../" not in text


def test_inbound_media_text_bounds_voice_and_sanitizes_windows_file_path():
    adapter = object.__new__(WeixinOCAdapter)
    text = adapter._message_text_from_item_list(
        [
            {"type": adapter.VOICE_ITEM_TYPE, "voice_item": {"text": "语" * 1200}},
            {
                "type": adapter.FILE_ITEM_TYPE,
                "file_item": {"file_name": "C:\\private\\bad\x00name.txt"},
            },
        ]
    )

    voice, file_name = text.splitlines()
    assert voice.startswith("[语音转写：") and voice.endswith("…]")
    assert len(voice) == 1007
    assert file_name == "[文件：badname.txt]"


@pytest.mark.asyncio
async def test_companion_action_sends_frozen_text_and_image_with_one_receipt(tmp_path):
    adapter = object.__new__(WeixinOCAdapter)
    temporary = tmp_path / "reaction.png"
    adapter._save_inbound_media = lambda *args, **kwargs: temporary
    adapter._prepare_media_item = AsyncMock(return_value={"type": adapter.IMAGE_ITEM_TYPE, "image_item": {}})
    adapter._send_items_with_receipt = AsyncMock(return_value=CLIENT_ID)

    def save(payload, **_kwargs):
        temporary.write_bytes(payload)
        return temporary

    adapter._save_inbound_media = save
    receipt = await adapter.send_companion_action(
        "wx-user",
        "看这个",
        {"asset_id": "asset", "mime": "image/png",
         "base64_data": base64.b64encode(PNG_1X1).decode("ascii")},
        CLIENT_ID,
    )

    assert receipt == CLIENT_ID
    assert not temporary.exists()
    prepared_path = adapter._prepare_media_item.await_args.args[1]
    assert isinstance(prepared_path, Path)
    sent_items = adapter._send_items_with_receipt.await_args.args[1]
    assert [item["type"] for item in sent_items] == [1, adapter.IMAGE_ITEM_TYPE]
    assert adapter._send_items_with_receipt.await_args.kwargs["client_id"] == CLIENT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("reaction", [
    {"asset_id": "asset", "mime": "image/png", "base64_data": "not-base64"},
    {"asset_id": "asset", "mime": "image/gif",
     "base64_data": base64.b64encode(PNG_1X1).decode("ascii")},
    {"asset_id": "asset", "mime": "text/plain",
     "base64_data": base64.b64encode(b"hello").decode("ascii")},
])
async def test_companion_action_rejects_invalid_or_mismatched_images(reaction):
    adapter = object.__new__(WeixinOCAdapter)
    adapter._send_items_with_receipt = AsyncMock()
    with pytest.raises(ValueError):
        await adapter.send_companion_action("wx-user", "", reaction, CLIENT_ID)
    adapter._send_items_with_receipt.assert_not_awaited()


@pytest.mark.asyncio
async def test_companion_action_sends_frozen_video_with_one_receipt(tmp_path):
    adapter = object.__new__(WeixinOCAdapter)
    temporary = tmp_path / "clip.mp4"

    def save(payload, **_kwargs):
        temporary.write_bytes(payload)
        return temporary

    adapter._save_inbound_media = save
    adapter._prepare_media_item = AsyncMock(
        return_value={"type": adapter.VIDEO_ITEM_TYPE, "video_item": {}}
    )
    adapter._send_items_with_receipt = AsyncMock(return_value=CLIENT_ID)
    media = {
        "asset_id": "video-1",
        "name": "clip.mp4",
        "kind": "video",
        "mime": "video/mp4",
        "sha256": hashlib.sha256(MP4).hexdigest(),
        "base64_data": base64.b64encode(MP4).decode("ascii"),
    }

    receipt = await adapter.send_companion_action(
        "wx-user", "", None, CLIENT_ID, media=media
    )

    assert receipt == CLIENT_ID
    assert not temporary.exists()
    assert adapter._prepare_media_item.await_args.args[2:5] == (
        adapter.VIDEO_UPLOAD_TYPE,
        adapter.VIDEO_ITEM_TYPE,
        "clip.mp4",
    )
    assert adapter._send_items_with_receipt.await_args.kwargs["cache_message_str"] == "[视频]"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["digest", "signature", "name", "kind"])
async def test_companion_action_rejects_tampered_frozen_video(change):
    payload = MP4 if change != "signature" else b"not-an-mp4-container"
    media = {
        "asset_id": "video-1",
        "name": "../clip.mp4" if change == "name" else "clip.mp4",
        "kind": "file" if change == "kind" else "video",
        "mime": "video/mp4",
        "sha256": ("0" * 64 if change == "digest" else hashlib.sha256(payload).hexdigest()),
        "base64_data": base64.b64encode(payload).decode("ascii"),
    }
    adapter = object.__new__(WeixinOCAdapter)
    adapter._send_items_with_receipt = AsyncMock()

    with pytest.raises(ValueError):
        await adapter.send_companion_action(
            "wx-user", "", None, CLIENT_ID, media=media
        )
    adapter._send_items_with_receipt.assert_not_awaited()
