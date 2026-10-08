import io
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.main as main
from app.main import app, rooms
from app.media import MAX_UPLOAD_BYTES, MediaStore, PhotoRejected, process_image

GPS_IFD = 0x8825
ORIENTATION = 0x0112


def jpeg_with_gps(size=(3000, 2000), orientation=None) -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[GPS_IFD] = {1: "N", 2: (55.0, 45.0, 0.0), 3: "E", 4: (37.0, 37.0, 0.0)}
    if orientation:
        exif[ORIENTATION] = orientation
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def image_bytes(fmt="PNG", size=(400, 300)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(output, format=fmt)
    return output.getvalue()


# ---- processing --------------------------------------------------------------------------


def test_exif_and_gps_are_removed_and_size_is_limited():
    original = jpeg_with_gps()
    assert Image.open(io.BytesIO(original)).getexif().get(GPS_IFD) is not None
    clean = process_image(original)
    with Image.open(io.BytesIO(clean)) as image:
        assert image.format == "JPEG"
        assert max(image.size) == 1024
        assert image.size == (1024, 683)
        assert len(image.getexif()) == 0
        assert "exif" not in image.info


def test_orientation_is_applied_before_exif_is_dropped():
    clean = process_image(jpeg_with_gps((2000, 1000), orientation=6))  # rotated 90° by the camera
    with Image.open(io.BytesIO(clean)) as image:
        assert image.size == (512, 1024)


def test_png_and_webp_become_jpeg_small_images_are_not_enlarged():
    for fmt in ("PNG", "WEBP"):
        with Image.open(io.BytesIO(process_image(image_bytes(fmt)))) as image:
            assert image.format == "JPEG" and image.size == (400, 300)


@pytest.mark.parametrize("data", [b"not an image at all", image_bytes("GIF"), image_bytes("BMP")],
                         ids=["text", "gif", "bmp"])
def test_non_images_and_other_formats_are_rejected(data):
    with pytest.raises(PhotoRejected):
        process_image(data)


def test_too_large_upload_is_rejected():
    with pytest.raises(PhotoRejected, match="8 MB"):
        process_image(b"\xff" * (MAX_UPLOAD_BYTES + 1))


def test_store_purges_old_photos_but_keeps_album(tmp_path):
    store = MediaStore(tmp_path)
    store.save("room_a", "old", b"1")
    store.save("room_a", "album", b"2")
    store.save("room_a", "fresh", b"3")
    past = time.time() - 25 * 3600
    for name in ("old", "album"):
        os.utime(store.path("room_a", name), (past, past))
    assert store.purge_older_than(keep={"album"}) == 1
    assert not store.path("room_a", "old").exists()
    assert store.path("room_a", "album").exists() and store.path("room_a", "fresh").exists()


# ---- full flow over the API ----------------------------------------------------------------


@pytest.fixture
def table(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "media", MediaStore(tmp_path))
    rooms.rooms.clear()
    client = TestClient(app)
    created = client.post("/api/rooms", json={"host_name": "Host"}).json()
    dealer = client.post("/api/join", json={"token": created["dealer_token"], "name": "Judge"}).json()
    players = [
        client.post("/api/join", json={"token": created["player_token"], "name": name, "agreed_to_terms": True}).json()
        for name in ("A", "B")
    ]
    room = rooms.get_room(created["room_id"])
    task = room.issue_task("player_1", task_type="photo", kind="bonus")
    return {"client": client, "created": created, "dealer": dealer, "players": players, "room": room,
            "task": task, "media_root": tmp_path}


def upload(client, token, task_id, data=None, content_type="image/jpeg"):
    return client.post("/api/photos", data={"token": token, "task_id": task_id},
                       files={"file": ("photo.jpg", data or jpeg_with_gps(), content_type)})


def can_see(client, url, token):
    return client.get(url, params={"token": token}).status_code == 200


def test_photo_cycle_access_rules_and_showcase(table):
    client, room, task = table["client"], table["room"], table["task"]
    author, other = (player["session_token"] for player in table["players"])
    dealer, host = table["dealer"]["session_token"], table["created"]["host_token"]

    response = upload(client, author, task["id"])
    assert response.status_code == 200
    url = response.json()["url"]
    assert task["status"] == "pending_review"
    stored = Image.open(table["media_root"] / room.room_id / f"{response.json()['photo_id']}.jpg")
    assert len(stored.getexif()) == 0

    # Under review: author and dealer only (section 10).
    assert can_see(client, url, author) and can_see(client, url, dealer)
    assert not can_see(client, url, host) and not can_see(client, url, other)
    assert "submission" not in room.view_for("player", "player_2")["tasks"][-1]
    assert room.view_for("host")["gallery"] == []

    room.review_task(task["id"], approved=True, comment="Шедевр")
    assert can_see(client, url, host) and can_see(client, url, other)
    assert room.view_for("host")["showcase"]["photo_id"] == response.json()["photo_id"]
    assert room.view_for("player", "player_2")["gallery"][0]["comment"] == "Шедевр"
    assert room.get_player("player_1").stack == 1000 + task["reward"]


def test_rejected_photo_stays_private_and_allows_one_retry(table):
    client, room, task = table["client"], table["room"], table["task"]
    author, other = (player["session_token"] for player in table["players"])
    url = upload(client, author, task["id"]).json()["url"]
    room.review_task(task["id"], approved=False)
    assert task["status"] == "pending"
    assert not can_see(client, url, other)
    room.last_upload_at.clear()
    assert upload(client, author, task["id"]).status_code == 200


def test_upload_is_validated(table):
    client, room, task = table["client"], table["room"], table["task"]
    author, other = (player["session_token"] for player in table["players"])
    assert upload(client, "bad-token", task["id"]).status_code == 401
    assert upload(client, other, task["id"]).status_code == 409
    assert upload(client, author, task["id"], b"GIF89a....", "image/gif").status_code == 415
    assert upload(client, author, task["id"], b"not really a jpeg").status_code == 415
    room.last_upload_at.clear()
    text_task = room.issue_task("player_1", task_type="text")
    assert upload(client, author, text_task["id"]).status_code == 409
    room.last_upload_at.clear()
    assert upload(client, author, task["id"]).status_code == 200
    # Second upload right away: task is no longer waiting for a photo.
    assert upload(client, author, task["id"]).status_code == 409


def test_upload_rate_limit(table):
    client, room = table["client"], table["room"]
    author = table["players"][0]["session_token"]
    first = room.issue_task("player_1", task_type="photo")
    second = room.issue_task("player_1", task_type="photo")
    assert upload(client, author, first["id"]).status_code == 200
    response = upload(client, author, second["id"])
    assert response.status_code == 409 and "wait" in response.json()["detail"]


def test_review_notice_goes_to_the_dealer_only(table):
    client, task = table["client"], table["task"]
    author = table["players"][0]["session_token"]
    other = table["players"][1]["session_token"]

    def collect(ws):
        messages = []
        while True:
            message = ws.receive_json()
            messages.append(message["type"])
            if message["type"] == "state":
                return messages

    with client.websocket_connect(f"/ws?token={table['dealer']['session_token']}") as dealer_ws, \
            client.websocket_connect(f"/ws?token={other}") as other_ws:
        collect(dealer_ws)
        collect(dealer_ws)  # state after the second connection
        collect(other_ws)
        assert upload(client, author, task["id"]).status_code == 200
        assert "photo_ready_for_review" in collect(dealer_ws)
        assert "photo_ready_for_review" not in collect(other_ws)


def test_reactions_album_zip_and_room_close(table):
    client, room, task, created = table["client"], table["room"], table["task"], table["created"]
    author = table["players"][0]["session_token"]
    photo_id = upload(client, author, task["id"]).json()["photo_id"]
    with pytest.raises(ValueError, match="No accepted photo"):
        room.react("player_2", task["id"], "🔥")
    room.review_task(task["id"], approved=True, comment="Огонь")
    room.react("player_2", task["id"], "🔥")
    room.react("player_2", task["id"], "😂")  # the last reaction counts
    room.react("player_1", task["id"], "😂")
    with pytest.raises(ValueError, match="Unknown reaction"):
        room.react("player_1", task["id"], "💩")
    assert room.view_for("player", "player_2")["gallery"][0]["reactions"] == {"😂": 2}

    second = room.issue_task("player_2", task_type="photo")
    other_photo = upload(client, table["players"][1]["session_token"], second["id"]).json()["photo_id"]
    room.review_task(second["id"], approved=True)
    room.save_to_album(photo_id)

    host = created["host_token"]
    assert client.get("/api/album.zip", params={"host_token": host}).status_code == 409  # game not over
    assert client.get("/api/album.zip", params={"host_token": author}).status_code == 404
    room.end_game()
    response = client.get("/api/album.zip", params={"host_token": host})
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = archive.namelist()
    assert len([name for name in names if name.endswith(".jpg")]) == 1
    assert "Огонь" in archive.read("album.txt").decode("utf-8")

    room_dir = table["media_root"] / room.room_id
    assert client.post(f"/api/rooms/{room.room_id}/close", json={"host_token": host}).json() == {"closed": True}
    assert (room_dir / f"{photo_id}.jpg").exists()
    assert not (room_dir / f"{other_photo}.jpg").exists()
    assert rooms.get_room(room.room_id) is None


def test_photo_tasks_are_hidden_without_human_dealer(table):
    room = table["room"]
    room.settings.dealer_mode = "ai"
    with pytest.raises(ValueError, match="human dealer"):
        room.issue_task("player_1", task_type="photo")
