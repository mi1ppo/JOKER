from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import socket
import time
import zipfile
from random import choice as random_choice
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, Optional

import qrcode
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import PROJECT_DIR, GigaChatConfig
from app.dealer.ai import AiDealer
from app.dealer.gigachat_client import GigaChatClient
from app.dealer.llm import LLMUnavailable
from app.game.events import random_event_id
from app.media import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES, MediaStore, PhotoRejected, process_image
from app.models import LobbySettings, tokens_equal
from app.rooms import RoomManager
from app.tasks.photo_tasks import new_photo_id
from app.views import state_for_connection

# Per-connection message limit (section 16).
WS_RATE_LIMIT = 20
WS_RATE_WINDOW_SEC = 1.0

rooms = RoomManager()
media = MediaStore()
log = logging.getLogger("chaos")
# Replaced at startup when GIGACHAT_AUTH_KEY is set; without a key every AI function uses fallback content.
ai = AiDealer(None)
background_tasks: set[asyncio.Task] = set()


def setup_logging() -> None:
    """Structured action log in logs/ (no hole cards, no secrets) plus the console."""
    logs_dir = PROJECT_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    root = logging.getLogger("chaos")
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(logs_dir / "chaos_poker.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console)


def spawn(coroutine) -> asyncio.Task:
    """AI calls never block the game: they run in the background (15.3)."""
    task = asyncio.create_task(coroutine)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def ai_in_use() -> bool:
    return any(room.settings.dealer_mode == "ai" for room in rooms.rooms.values())


async def start_ai(config: GigaChatConfig) -> None:
    """Startup check (15.4): token, GET /models, the configured model exists."""
    global ai
    if not config.enabled:
        ai = AiDealer(None)
        ai.status.update(message="Ключ GigaChat не задан — играем на резервном контенте")
        log.info("ИИ-судья: ключ не задан, ИИ-функции отключены")
        return
    if not config.verify_ssl:
        log.warning("ВНИМАНИЕ: GIGACHAT_VERIFY_SSL=false — проверка сертификата GigaChat отключена (только для отладки)")
    elif config.ca_bundle_missing:
        log.warning("Нет сертификата Минцифры (%s): запросы к GigaChat могут не пройти проверку", config.ca_bundle)
    client = GigaChatClient(config, in_use=ai_in_use)
    ai = AiDealer(client, config.token_budget)
    await client.tokens.start()
    await refresh_ai_status()
    log.info("ИИ-судья: %s", ai.status["message"])


async def refresh_ai_status() -> dict:
    if not isinstance(ai.client, GigaChatClient):
        return ai.status
    result = await ai.client.check()
    ai.status.update(enabled=True, ready=result["ready"], reason=result["reason"], message=result["message"],
                     models=result.get("models"))
    return ai.status
MEDIA_PURGE_EVERY_SEC = 3600
# room_id -> websocket -> (role, player_id)
connections: dict[str, dict[WebSocket, tuple[str, str | None]]] = {}
static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")


async def enforce_timers() -> None:
    last_purge = 0.0
    while True:
        await asyncio.sleep(1)
        for room_id in rooms.process_timers():
            room = rooms.get_room(room_id)
            if room is not None:
                await broadcast_state(room)
        if time.monotonic() - last_purge > MEDIA_PURGE_EVERY_SEC:
            last_purge = time.monotonic()
            # Photos older than 24 hours are deleted, album photos are kept (section 16).
            keep = {photo_id for room in rooms.rooms.values() for photo_id in room.album_photo_ids()}
            await run_in_threadpool(media.purge_older_than, keep=keep)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    try:
        await start_ai(GigaChatConfig.from_env())
    except Exception as error:  # AI problems must never stop the server
        log.warning("ИИ-судья недоступен: %s", type(error).__name__)
    task = asyncio.create_task(enforce_timers())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if isinstance(ai.client, GigaChatClient):
            await ai.client.aclose()


app = FastAPI(title="Chaos Poker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class RoomCreateRequest(BaseModel):
    host_name: str = Field(min_length=1, max_length=40)


class JoinRequest(BaseModel):
    token: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=40)
    avatar: str = Field(default="🃏", max_length=4)
    agreed_to_terms: bool = False


class HostActionRequest(BaseModel):
    host_token: str = Field(min_length=1)


class DealerSessionRequest(BaseModel):
    session_token: str = Field(min_length=1)


class ActivityRequest(BaseModel):
    host_token: str = Field(min_length=1)
    activity_type: str = Field(min_length=1, max_length=40)


class HostCommentRequest(BaseModel):
    context: str = Field(default="", max_length=400)


class RoomEventRequest(BaseModel):
    host_token: str | None = None
    session_token: str | None = None
    event_id: str = Field(min_length=1, max_length=40)


class RoomTaskRequest(BaseModel):
    host_token: str | None = None
    session_token: str | None = None
    player_id: str = Field(min_length=1, max_length=40)
    task_type: str = Field(default="text", max_length=20)
    kind: str = Field(default="penalty", max_length=20)
    difficulty: str = Field(default="medium", max_length=20)
    camera: str | None = Field(default=None, max_length=10)
    source: str | None = Field(default=None, max_length=10)
    text: str | None = Field(default=None, max_length=300)


class PhotoReviewRequest(BaseModel):
    host_token: str | None = None
    session_token: str | None = None
    approved: bool = True
    comment: str | None = Field(default=None, max_length=200)


def _room_payload(room) -> dict:
    return {
        "room_id": room.room_id,
        "host_token": room.host_token,
        "dealer_token": room.dealer_token,
        "player_token": room.player_token,
        "state": room.host_view(),
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/rooms")
def create_room(request: RoomCreateRequest) -> dict:
    return _room_payload(rooms.create_room(host_name=request.host_name))


@app.get("/api/session")
def session_state(token: str) -> dict:
    room, role, player_id = rooms.find_session(token)
    if room is None or role is None:
        raise HTTPException(status_code=404, detail="Invalid or expired session")
    return {
        "room_id": room.room_id,
        "role": role,
        "player_id": player_id,
        "state": state_for_connection(room, role, player_id),
    }


def get_staff_room(room_id: str, *, host_token: str | None = None, session_token: str | None = None):
    """Room for the host or the connected dealer (one-time join tokens are not accepted)."""
    room = rooms.get_room(room_id)
    token = host_token or session_token
    if room is None or token is None:
        raise HTTPException(status_code=404, detail="Room not found")
    if tokens_equal(room.host_token, token) or tokens_equal(room.dealer_session_token, token):
        return room
    raise HTTPException(status_code=404, detail="Room not found")


def get_host_room(room_id: str, host_token: str):
    room = rooms.get_room(room_id)
    if room is None or not tokens_equal(room.host_token, host_token):
        raise HTTPException(status_code=404, detail="Room not found")
    return room


@app.post("/api/rooms/{room_id}/settings")
async def update_settings(room_id: str, settings: LobbySettings, host_token: str):
    room = get_host_room(room_id, host_token)
    try:
        room.update_settings(settings)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if settings.dealer_mode == "ai" and isinstance(ai.client, GigaChatClient):
        spawn(ai.client.tokens.warm_up())  # AI is coming back: get the token ready in advance
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/start")
async def start_game(room_id: str, request: HostActionRequest):
    room = get_host_room(room_id, request.host_token)
    try:
        room.start_game()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/end")
async def end_game(room_id: str, request: HostActionRequest):
    room = get_host_room(room_id, request.host_token)
    room.end_game()
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/ai-comment")
async def ai_comment(room_id: str, request: HostCommentRequest, host_token: str):
    """A comment of the judge on the last hand (AI or fallback)."""
    room = get_host_room(room_id, host_token)
    comment, source = await ai.comment(room, room.last_hand or {"winnings": {}})
    room.add_dealer_comment(comment, source)
    await broadcast_state(room)
    return {"comment": comment, "source": source, "state": room.host_view()}


@app.get("/api/ai/status")
def ai_status(host_token: str):
    room, role, _ = rooms.find_session(host_token)
    if room is None or role != "host":
        raise HTTPException(status_code=404, detail="Room not found")
    return {**ai.status, "usage": ai.budget(room).view()}


@app.post("/api/ai/check")
async def ai_check(request: HostActionRequest):
    """The "check AI" button in the host lobby: repeats the startup check."""
    room, role, _ = rooms.find_session(request.host_token)
    if room is None or role != "host":
        raise HTTPException(status_code=404, detail="Room not found")
    status = await refresh_ai_status()
    await broadcast_state(room)
    return {**status, "usage": ai.budget(room).view()}


@app.post("/api/rooms/{room_id}/pause")
async def pause_room(room_id: str, request: DealerSessionRequest):
    room = get_staff_room(room_id, session_token=request.session_token)
    room.pause_game()
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/resume")
async def resume_room(room_id: str, request: DealerSessionRequest):
    room = get_staff_room(room_id, session_token=request.session_token)
    room.resume_game()
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/activity")
async def start_activity_endpoint(room_id: str, request: ActivityRequest):
    room = get_host_room(room_id, request.host_token)
    try:
        room.start_activity(request.activity_type)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/event")
async def select_event(room_id: str, request: RoomEventRequest):
    room = get_staff_room(room_id, host_token=request.host_token, session_token=request.session_token)
    try:
        room.select_event(request.event_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/rooms/{room_id}/tasks")
async def issue_task(room_id: str, request: RoomTaskRequest):
    room = get_staff_room(room_id, host_token=request.host_token, session_token=request.session_token)
    try:
        if request.source == "ai":
            request_ai_task(room, request.player_id, kind=request.kind, photo=request.task_type == "photo",
                            difficulty=request.difficulty)
        else:
            room.issue_task(
                request.player_id,
                task_type=request.task_type,
                kind=request.kind,
                difficulty=request.difficulty,
                camera=request.camera,
                title=request.text,
                source=request.source,
            )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await broadcast_state(room)
    return {"state": room.host_view()}


def request_ai_task(room, player_id: str, *, kind: str, photo: bool, difficulty: str) -> None:
    """Validate now, generate in the background, issue when the text is ready (bank on failure)."""
    if photo and room.settings.dealer_mode != "human":
        raise ValueError("Photo tasks are available only with a human dealer")
    if kind not in {"penalty", "bonus"}:
        raise ValueError(f"Unknown task kind: {kind}")
    if player_id == "random":
        candidates = [player.id for player in room.players if player.stack > 0] or [player.id for player in room.players]
        if not candidates:
            raise ValueError("No players")
        player_id = random_choice(candidates)
    room.get_player(player_id)
    room.request_ai("task", player_id=player_id, task_kind=kind, photo=photo, difficulty=difficulty, source="ai")


@app.post("/api/photos")
async def upload_photo(token: str = Form(...), task_id: str = Form(..., max_length=40), file: UploadFile = File(...)):
    """Photo for a photo task (multipart: task_id, file), authorised by the player's session token."""
    room, role, player_id = rooms.find_session(token)
    if room is None or role != "player" or player_id is None:
        raise HTTPException(status_code=401, detail="Invalid session")
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Only JPEG, PNG and WebP photos are accepted")
    try:
        room.check_upload_allowed(player_id, task_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Photo is larger than 8 MB")
    try:
        jpeg = await run_in_threadpool(process_image, data)
    except PhotoRejected as error:
        raise HTTPException(status_code=415, detail=str(error)) from error
    photo_id = new_photo_id()
    await run_in_threadpool(media.save, room.room_id, photo_id, jpeg)
    try:
        room.attach_photo(player_id, task_id, photo_id)
    except ValueError as error:
        media.delete(room.room_id, photo_id)
        raise HTTPException(status_code=409, detail=str(error)) from error
    await broadcast_state(room)
    return {"photo_id": photo_id, "url": f"/media/{photo_id}"}


@app.get("/media/{photo_id}")
def get_photo(photo_id: str, token: str):
    """A photo for the author, the dealer, and everybody in the room after it was accepted."""
    room, role, player_id = rooms.find_session(token)
    if room is None or role is None or not room.photo_visible(photo_id, role, player_id):
        raise HTTPException(status_code=404, detail="Photo not found")
    path = media.path(room.room_id, photo_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Photo not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/album.zip")
def album_zip(host_token: str):
    """The evening album as a .zip (host only, after the game is over)."""
    room, role, _ = rooms.find_session(host_token)
    if room is None or role != "host":
        raise HTTPException(status_code=404, detail="Room not found")
    if not room.game_over:
        raise HTTPException(status_code=409, detail="The album is available after the game")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        lines = []
        for index, entry in enumerate(room.album_entries(), start=1):
            path = media.path(room.room_id, entry["photo_id"])
            if not path.exists():
                continue
            safe_title = "".join(char for char in entry["title"] if char.isalnum() or char in " -_").strip()[:40]
            name = f"{index:02d} {safe_title or 'photo'}.jpg"
            archive.write(path, name)
            comment = f" — {entry['comment']}" if entry["comment"] else ""
            lines.append(f"{name}: {entry['player']}, «{entry['title']}»{comment}")
        archive.writestr("album.txt", "Альбом вечера Chaos Poker\n\n" + "\n".join(lines) + "\n")
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="chaos_poker_album.zip"'})


@app.post("/api/rooms/{room_id}/close")
async def close_room(room_id: str, request: HostActionRequest):
    """Close the room: everybody is disconnected and the photos (except the album) are deleted."""
    room = get_host_room(room_id, request.host_token)
    for websocket in list(connections.pop(room.room_id, {})):
        with contextlib.suppress(Exception):
            await websocket.close(code=1000)
    await run_in_threadpool(media.delete_room, room.room_id, room.album_photo_ids())
    rooms.close_room(room.room_id)
    return {"closed": True}


@app.post("/api/rooms/{room_id}/tasks/{task_id}/review")
async def review_photo_task(room_id: str, task_id: str, request: PhotoReviewRequest):
    room = get_staff_room(room_id, host_token=request.host_token, session_token=request.session_token)
    try:
        room.review_task(task_id, approved=request.approved, comment=request.comment)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    await broadcast_state(room)
    return {"state": room.host_view()}


@app.post("/api/join")
async def join_room(request: JoinRequest) -> dict:
    room, role = rooms.find_by_token(request.token)
    # Only one-time join tokens are accepted here.
    if room is None or role is None or not (
        tokens_equal(room.dealer_token, request.token) or tokens_equal(room.player_token, request.token)
    ):
        raise HTTPException(status_code=404, detail="Invalid or expired token")
    if role == "dealer":
        room.connect_dealer()
        await broadcast_state(room)
        return {
            "room_id": room.room_id,
            "role": "dealer",
            "session_token": room.dealer_session_token,
            "state": room.view_for("dealer"),
        }
    try:
        player = room.add_player(
            name=request.name,
            avatar=request.avatar,
            agreed_to_terms=request.agreed_to_terms,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    payload = {
        "room_id": room.room_id,
        "role": "player",
        "player_id": player.id,
        "session_token": player.session_token,
        "state": room.view_for("player", player.id),
    }
    await broadcast_state(room)
    return payload


def public_base_url() -> str:
    """Base of the links in QR codes: PUBLIC_URL (a tunnel, e.g. Cloudflare) or https://<LAN IP>:8443."""
    configured = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
    return configured or f"https://{local_ip()}:{os.getenv('PORT', '8443')}"


def local_ip() -> str:
    configured_ip = os.getenv("HOST_IP")
    if configured_ip:
        return configured_ip
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


async def _send(room, websocket: WebSocket, message: dict[str, Any]) -> None:
    try:
        await websocket.send_json(message)
    except Exception:
        connections.get(room.room_id, {}).pop(websocket, None)


async def run_ai_request(room, request: dict) -> None:
    """One queued AI request; whatever happens, the room gets an answer (AI or fallback)."""
    kind = request["kind"]
    try:
        if kind == "comment":
            text, source = await ai.comment(room, request["hand"])
            room.add_dealer_comment(text, source)
        elif kind == "event":
            event_id, explanation, _ = await ai.choose_event(room)
            room.apply_ai_event(event_id, explanation, request["for_hand"])
        elif kind == "task":
            fields, source = await ai.generate_task(room, request["player_id"], kind=request["task_kind"],
                                                    photo=request["photo"], difficulty=request["difficulty"])
            room.issue_task(request["player_id"], kind=request["task_kind"],
                            task_type="photo" if request["photo"] else "text", difficulty=request["difficulty"],
                            title=fields["title"], description=fields["description"],
                            time_limit_sec=fields["time_limit_sec"], camera=fields.get("camera"),
                            source=request.get("source") if source == "ai" else "bank")
        elif kind == "bribe":
            bribe = room.get_bribe(request["bribe_id"])
            decision, _ = await ai.bribe(room, bribe["player_id"], bribe["amount"], bribe["wish"])
            room.resolve_bribe(bribe["id"], decision)
        elif kind == "protocol":
            protocol, source = await ai.protocol(room)
            room.set_protocol(protocol, source)
    except (ValueError, LLMUnavailable) as error:
        log.info("AI request %s skipped: %s", kind, error)
    except Exception:
        log.exception("AI request %s failed", kind)
    if rooms.get_room(room.room_id) is room:
        await broadcast_state(room)


async def broadcast_state(room) -> None:
    for request in room.pop_ai_requests():
        spawn(run_ai_request(room, request))
    room.ai_status = {**ai.status, "usage": ai.budget(room).view()}
    # One-off notifications first (animations), then the filtered snapshot.
    for message_type, payload in room.pop_notices():
        await broadcast(room, message_type, payload)
    for audience, message_type, payload in room.pop_direct_notices():
        await send_to(room, audience, message_type, payload)
    for websocket, (role, player_id) in list(connections.get(room.room_id, {}).items()):
        await _send(room, websocket, {"type": "state", "payload": state_for_connection(room, role, player_id)})


async def send_to(room, audience: str, message_type: str, payload: dict[str, Any]) -> None:
    """Notification for one audience: "host", "dealer" or a player id."""
    for websocket, (role, player_id) in list(connections.get(room.room_id, {}).items()):
        if audience in {role, player_id}:
            await _send(room, websocket, {"type": message_type, "payload": payload})


async def broadcast(room, message_type: str, payload: dict[str, Any]) -> None:
    """Send a public notification (for animations) to every connection of the room."""
    for websocket in list(connections.get(room.room_id, {})):
        await _send(room, websocket, {"type": message_type, "payload": payload})


@app.get("/{role}")
def page(role: str):
    if role not in {"host", "dealer", "play"}:
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(os.path.join(static_dir, f"{role}.html"))


@app.get("/api/qr/{kind}.png")
def qr_code(kind: str, room_id: str, host_token: Optional[str] = None) -> StreamingResponse:
    room = rooms.get_room(room_id)
    if room is None or not host_token or not tokens_equal(room.host_token, host_token) or kind not in {"dealer", "players"}:
        raise HTTPException(status_code=404, detail="QR code not found")

    token = room.dealer_token if kind == "dealer" else room.player_token
    if not token:
        raise HTTPException(status_code=410, detail="Token expired")

    target = "dealer" if kind == "dealer" else "play"
    url = f"{public_base_url()}/{target}?token={token}"
    return _qr_response(url)


@app.get("/api/host-qr")
def generate_host_qr(host_token: str) -> StreamingResponse:
    """Generate a QR code for the host to join the game."""
    room, role, _ = rooms.find_session(host_token)
    if room is None or role != "host":
        raise HTTPException(status_code=404, detail="Room not found")
    return _qr_response(f"{public_base_url()}/host?token={host_token}")


def _qr_response(url: str) -> StreamingResponse:
    image = qrcode.make(url)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return StreamingResponse(output, media_type="image/png")


# Payload fields that are safe to log (section 16: no hole cards, no secrets, no free texts).
LOGGED_FIELDS = ("kind", "amount", "ability_id", "target_id", "bet_id", "task_id", "subject_id", "value",
                 "event_id", "decree_id", "type", "wish", "accepted", "choice", "winner_id")
action_log = logging.getLogger("chaos.actions")


def log_action(room, role: str, player_id: str | None, message: dict) -> None:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    safe = {key: payload[key] for key in LOGGED_FIELDS if key in payload}
    action_log.info("room=%s role=%s player=%s action=%s %s", room.room_id, role, player_id or "-",
                    message.get("type"), safe)


PLAYER_MESSAGES = {"action", "use_ability", "carousel_pick", "side_bet", "task_done", "task_refuse", "vote", "react",
                   "activity_report", "duel_pick", "activity_skip", "bribe"}
DEALER_MESSAGES = {"dealer_issue_task", "dealer_review", "dealer_decree", "dealer_fine", "dealer_pause", "activity_resolve"}
STAFF_MESSAGES = {"dealer_start_activity", "activity_cancel"}


class ClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def handle_message(room, websocket: WebSocket, role: str, player_id: str | None, message: dict) -> None:
    """Apply one client -> server message (section 14.1). The role comes from the token, never from the client."""
    message_type = message.get("type")
    payload = message.get("payload") or {}
    if not isinstance(payload, dict):
        raise ClientError("bad_request", "payload must be an object")

    if message_type in PLAYER_MESSAGES and (role != "player" or player_id is None):
        raise ClientError("forbidden", "Only players can do this")
    if message_type in {"host_start_game", "host_pause", "host_resume", "host_end"} and role != "host":
        raise ClientError("forbidden", "Only the host can do this")
    if message_type == "dealer_trigger_event" and role not in {"dealer", "host"}:
        raise ClientError("forbidden", "Only the dealer can do this")
    if message_type in DEALER_MESSAGES and role != "dealer":
        raise ClientError("forbidden", "Only the dealer can do this")
    if message_type in STAFF_MESSAGES and role not in {"dealer", "host"}:
        raise ClientError("forbidden", "Only the dealer or the host can do this")

    try:
        if message_type == "action":
            amount = payload.get("amount")
            if amount is not None and not isinstance(amount, int):
                raise ClientError("bad_request", "amount must be an integer")
            room.apply_player_action(player_id, str(payload.get("kind")), amount)
        elif message_type == "use_ability":
            card_index = payload.get("card_index")
            result = room.use_ability(
                player_id,
                str(payload.get("ability_id")),
                target_id=payload.get("target_id"),
                index=card_index if isinstance(card_index, int) else None,
            )
            public = {key: value for key, value in result.items() if key in {"ability", "target_id"}}
            await broadcast(room, "ability_used", {"player_id": player_id, **public})
            await _send(room, websocket, {"type": "private_notice", "payload": result})
        elif message_type == "task_done":
            room.complete_task(player_id, str(payload.get("task_id")))
        elif message_type == "task_refuse":
            room.refuse_task(player_id, str(payload.get("task_id")))
        elif message_type == "vote":
            room.vote_task(player_id, str(payload.get("subject_id")), bool(payload.get("value")))
        elif message_type == "dealer_issue_task":
            target = str(payload.get("target_id") or "random")
            difficulty = str(payload.get("difficulty") or room.settings.task_spiciness)
            if payload.get("source") == "ai":
                request_ai_task(room, target, kind=str(payload.get("kind") or "penalty"),
                                photo=bool(payload.get("photo")), difficulty=difficulty)
            else:
                room.issue_task(
                    target,
                    kind=str(payload.get("kind") or "penalty"),
                    source=str(payload.get("source") or "bank"),
                    title=payload.get("text") if payload.get("source") == "custom" else None,
                    task_type="photo" if payload.get("photo") else "text",
                    difficulty=difficulty,
                )
        elif message_type == "bribe":
            amount = payload.get("amount")
            if not isinstance(amount, int):
                raise ClientError("bad_request", "amount must be an integer")
            room.start_bribe(player_id, amount, str(payload.get("wish")))
        elif message_type == "dealer_review":
            comment = payload.get("comment")
            room.review_task(str(payload.get("task_id")), approved=bool(payload.get("accepted")),
                             comment=comment if isinstance(comment, str) else None)
        elif message_type == "dealer_start_activity":
            players = payload.get("players")
            if players is not None and not (isinstance(players, list) and all(isinstance(item, str) for item in players)):
                raise ClientError("bad_request", "players must be a list of ids")
            room.start_activity(str(payload.get("type")), players=players or None)
        elif message_type == "activity_resolve":
            movers = payload.get("movers")
            room.resolve_activity(winner_id=payload.get("winner_id"),
                                  movers=[str(item) for item in movers] if isinstance(movers, list) else None)
        elif message_type == "activity_report":
            moved = payload.get("moved")
            room.report_activity(player_id, winner_id=payload.get("winner_id"),
                                 moved=bool(moved) if moved is not None else None)
        elif message_type == "duel_pick":
            room.duel_pick(player_id, str(payload.get("choice")))
        elif message_type == "activity_skip":
            room.skip_activity(player_id)
        elif message_type == "activity_cancel":
            room.cancel_activity()
        elif message_type == "react":
            room.react(player_id, str(payload.get("task_id")), str(payload.get("emoji")))
        elif message_type == "album_save":
            room.save_to_album(str(payload.get("photo_id")))
        elif message_type == "dealer_decree":
            room.issue_decree(str(payload.get("decree_id")), payload.get("text"))
        elif message_type == "dealer_fine":
            amount = payload.get("amount")
            if not isinstance(amount, int):
                raise ClientError("bad_request", "amount must be an integer")
            room.fine_player(str(payload.get("target_id")), amount, reason=payload.get("reason"))
        elif message_type == "dealer_pause":
            if room.paused:
                room.resume_game()
            else:
                room.pause_game()
        elif message_type == "side_bet":
            room.place_side_bet(player_id, str(payload.get("bet_id")))
        elif message_type == "carousel_pick":
            card_index = payload.get("card_index")
            if not isinstance(card_index, int):
                raise ClientError("bad_request", "card_index must be an integer")
            room.pick_carousel(player_id, card_index)
        elif message_type == "host_start_game":
            room.start_game()
        elif message_type == "host_pause":
            room.pause_game()
        elif message_type == "host_resume":
            room.resume_game()
        elif message_type == "host_end":
            room.end_game()
        elif message_type == "dealer_trigger_event":
            # Queued for the next hand; revealed to everybody in the EVENT_REVEAL phase.
            event_id = payload.get("event_id") or random_event_id()
            event = room.select_event(str(event_id))
            await _send(room, websocket, {"type": "private_notice", "payload": {"queued_event": event}})
        else:
            raise ClientError("unknown_type", f"Unknown message type: {message_type}")
    except ValueError as error:
        raise ClientError("invalid", str(error)) from error


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str) -> None:
    await websocket.accept()
    room, role, player_id = rooms.find_session(token)
    if room is None or role is None:
        await websocket.send_json({"type": "error", "payload": {"code": "unauthorized", "message": "Unknown room"}})
        await websocket.close(code=1008)
        return

    connections.setdefault(room.room_id, {})[websocket] = (role, player_id)
    room.set_connected(player_id, True)
    recent: deque[float] = deque()

    try:
        await broadcast_state(room)
        while True:
            try:
                message = await websocket.receive_json()
            except ValueError:
                await websocket.send_json({"type": "error", "payload": {"code": "bad_request", "message": "Invalid JSON"}})
                continue
            now = time.monotonic()
            while recent and now - recent[0] > WS_RATE_WINDOW_SEC:
                recent.popleft()
            recent.append(now)
            if len(recent) > WS_RATE_LIMIT:
                await websocket.send_json({"type": "error", "payload": {"code": "rate_limited", "message": "Too many messages"}})
                continue
            if not isinstance(message, dict):
                await websocket.send_json({"type": "error", "payload": {"code": "bad_request", "message": "Invalid envelope"}})
                continue
            try:
                await handle_message(room, websocket, role, player_id, message)
            except ClientError as error:
                await websocket.send_json({
                    "type": "error",
                    "payload": {"code": error.code, "message": error.message},
                    "msg_id": message.get("msg_id"),
                })
                continue
            log_action(room, role, player_id, message)
            await broadcast_state(room)
    except WebSocketDisconnect:
        pass
    finally:
        room_connections = connections.get(room.room_id, {})
        room_connections.pop(websocket, None)
        if player_id is not None and not any(pid == player_id for _, pid in room_connections.values()):
            room.set_connected(player_id, False)
            await broadcast_state(room)
