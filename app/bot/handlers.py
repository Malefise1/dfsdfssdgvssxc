"""Composition root for bot screens; account operations live in separate modules."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from sqlalchemy import func, select

from app.bot import accounts, admin, reporting, work
from app.bot.common import BotServices, Gate, Input, home_row, kb, owner, require_owner, show
from app.enums import AccountStatus, JobStatus
from app.models import Channel, Job, TelegramAccount
from app.services.control import paused

__all__ = ["BotServices", "create_router"]


def create_router(s):
    router = Router(name="bot")
    gate = Gate(s)
    router.message.outer_middleware(gate)
    router.callback_query.outer_middleware(gate)
    account_router, account_text, account_file = accounts.create_router(s)
    work_router, work_text = work.create_router(s)
    report_router, report_text = reporting.create_router(s)
    admin_router, admin_file, admin_text = admin.create_router(s)

    async def home(event, state):
        await state.clear()
        async with s.db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(TelegramAccount)
                .where(TelegramAccount.archived_at.is_(None))
            )
            active = await session.scalar(
                select(func.count())
                .select_from(TelegramAccount)
                .where(
                    TelegramAccount.archived_at.is_(None),
                    TelegramAccount.status == AccountStatus.ACTIVE,
                )
            )
            channels = await session.scalar(
                select(func.count()).select_from(Channel).where(Channel.archived_at.is_(None))
            )
            jobs = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
            )
        is_paused = await paused(s.db)
        rows = [
            [("Запустить", "menu:work", 16, "success")],
            [("Аккаунты", "accounts:all:0", 3, "primary"), ("Каналы", "channels:0", 25, "primary")],
            [("Отчёты", "reports:all:0", 30), ("Расписания", "schedules:0", 21)],
        ]
        if owner(s, event.from_user.id):
            rows += [
                [("Прокси", "own:proxies:0", 28, "primary"), ("Настройки", "own:settings", 104)],
                [("Возобновить работу", "own:resume", 69)]
                if is_paused
                else [("Остановить всё", "own:stop", 125)],
            ]
        await show(
            event,
            f"<b>Панель управления</b>\n{'⏸ На паузе' if is_paused else '🟢 Готов к работе'}\n\n"
            f"👤 Аккаунты: {active}/{count} активны\n📣 Каналы: {channels}\n⚡ Задания: {jobs}",
            kb(*rows),
        )

    @router.message(Command("start", "menu"))
    async def start(message, state):
        await home(message, state)

    @router.callback_query(F.data == "menu:home")
    async def home_callback(c, state):
        await home(c, state)

    async def help_message(event):
        await show(
            event,
            "<b>Коротко</b>\nЗапустить → режим → каналы → подтверждение.\n"
            "Результаты и XLSX — в «Отчётах».\n"
            "Зелёные кнопки — запуск и добавление, красные — остановка и удаление.\n\n"
            "/menu — меню · /cancel — отмена ввода\n"
            "Время: " + escape(s.settings.timezone),
            kb(home_row()),
        )

    @router.message(Command("help"))
    async def help_command(message):
        await help_message(message)

    @router.callback_query(F.data == "menu:help")
    async def help_callback(c):
        await help_message(c)

    @router.message(Command("cancel"))
    async def cancel(message, state):
        await state.clear()
        s.onboarding.cancel(message.from_user.id)
        await message.answer(
            "Ввод отменён. Добавление остановлено, уже сохранённые аккаунты остаются.",
            reply_markup=kb(home_row()),
        )

    async def stop(event):
        require_owner(s, event.from_user.id)
        s.onboarding.cancel(event.from_user.id)
        count = await s.runner.stop_all(event.from_user.id)
        # Cancel non-job account checks/subscription operations still waiting to start.
        for task in list(s.background):
            if task.get_name() == "check-accounts":
                task.cancel()
        await show(
            event,
            f"⏸ Общая пауза. Останавливаю заданий: {count}.\nАвтозапуски отключены до возобновления.",
            kb([("Возобновить", "own:resume", 69)], home_row()),
        )

    @router.callback_query(F.data == "own:stop")
    async def stop_callback(c):
        await stop(c)

    @router.message(Command("stop_all"))
    async def stop_command(message):
        await stop(message)

    async def resume(event):
        require_owner(s, event.from_user.id)
        await s.runner.resume_permanent(event.from_user.id)
        await show(
            event,
            "▶ Работа возобновлена. Расписания и 24/7 снова включены.",
            kb(home_row()),
        )

    @router.callback_query(F.data == "own:resume")
    async def resume_callback(c):
        await resume(c)

    @router.message(Command("resume_all"))
    async def resume_command(message):
        await resume(message)

    @router.message(Input.text, F.text, ~F.text.startswith("/"))
    async def text(message, state):
        data = await state.get_data()
        for handler in (account_text, work_text, report_text, admin_text):
            if await handler(message, state, data):
                return
        await state.clear()
        await message.answer(
            "Ввод устарел. Откройте нужный раздел заново.", reply_markup=kb(home_row())
        )

    @router.message(Input.file, F.text, ~F.text.startswith("/"))
    async def file_text(message, state):
        data = await state.get_data()
        if not await admin_text(message, state, data):
            await message.answer("Пришлите файл или нажмите /cancel.")

    @router.message(Input.file, F.document)
    async def file(message, state):
        data = await state.get_data()
        for handler in (account_file, admin_file):
            if await handler(message, state, data):
                return
        await state.clear()
        await message.answer("Сначала выберите вид загрузки в меню.", reply_markup=kb(home_row()))

    router.include_routers(account_router, work_router, report_router, admin_router)
    fallback = Router(name="fallback")

    @fallback.callback_query()
    async def expired(c, state):
        await state.clear()
        await show(c, "Кнопка устарела. Откройте главное меню.", kb(home_row()))

    @fallback.message()
    async def unknown(message):
        if message.document:
            await message.answer(
                "Для загрузки выберите «Добавить аккаунты → Файл» или «Прокси → Загрузить». Если прислали секретный файл, удалите это сообщение."
            )
        else:
            await message.answer(
                "Выберите действие в меню. /cancel отменяет текущий ввод.",
                reply_markup=kb(home_row()),
            )

    router.include_router(fallback)
    return router
