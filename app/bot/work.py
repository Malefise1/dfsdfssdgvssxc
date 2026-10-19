from __future__ import annotations

import secrets
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from sqlalchemy import func, or_, select, update

from app.bot.common import PAGE, Input, date, home_row, kb, owner, pages, require_owner, show
from app.enums import AccountStatus, JobMode, JobStatus, ScheduleKind
from app.models import Channel, Job, JobChannel, Schedule, TelegramAccount, utcnow
from app.repositories import upsert_channel
from app.services.access import channel_scope, require_channels
from app.services.control import audit, paused
from app.telegram.links import parse_channel_links

MODES = {"manual": JobMode.MANUAL_FULL, "permanent": JobMode.PERMANENT_CYCLE, "test": JobMode.TEST}
MODE_LABELS = {
    "manual": "полный проход",
    "permanent": "постоянная работа 24/7",
    "test": "тест по времени",
}


def create_router(s):
    router = Router(name="work")

    async def add_links(links, actor):
        scope = await channel_scope(s.settings, s.db, actor)
        async with s.db.session() as session:
            if scope is not None:
                rows = list(
                    (
                        await session.scalars(
                            select(Channel).where(
                                func.lower(Channel.username).in_(
                                    [link.username.lower() for link in links]
                                ),
                                Channel.archived_at.is_(None),
                                Channel.id.in_(scope),
                            )
                        )
                    ).all()
                )
                if len(rows) != len(links):
                    raise ValueError("Часть ссылок не входит в доступ менеджера")
                return [row.id for row in rows]
            return [
                (await upsert_channel(session, link.username, link.url, actor)).id for link in links
            ]

    async def channels(event, state, page=0):
        data = await state.get_data()
        search = data.get("channel_search", "")
        query = select(Channel).where(Channel.archived_at.is_(None))
        scope = await channel_scope(s.settings, s.db, event.from_user.id)
        if scope is not None:
            query = query.where(Channel.id.in_(scope))
        if search:
            query = query.where(
                or_(
                    Channel.username.icontains(search.lstrip("@"), autoescape=True),
                    Channel.title.icontains(search, autoescape=True),
                )
            )
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(Channel.id).offset(max(0, page) * PAGE).limit(PAGE + 1)
                    )
                ).all()
            )
        await show(
            event,
            f"<b>Каналы</b> · {page + 1}" + (f"\nПоиск: {escape(search)}" if search else ""),
            kb(
                [("Добавить ссылки", "channel:add", 59), ("Найти", "channel:search", 123)],
                *[
                    [
                        (
                            f"@{r.username} · {'🟢 24/7' if r.permanent and r.active else '⏸ 24/7' if r.permanent else 'по запросу'}",
                            f"channel:{r.id}",
                            25,
                        )
                    ]
                    for r in rows[:PAGE]
                ],
                pages("channels", page, len(rows) > PAGE),
                [("Сбросить поиск", "channel:reset")],
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("channels:"))
    async def channels_page(c, state):
        await channels(c, state, int(c.data.rsplit(":", 1)[1]))

    @router.callback_query(F.data.in_({"channel:add", "channel:search"}))
    async def channel_input(c, state):
        await state.set_state(Input.text)
        await state.update_data(
            action="channel_add" if c.data == "channel:add" else "channel_search"
        )
        await show(
            c,
            "Пришлите публичные ссылки t.me или @username через пробел или с новой строки."
            if c.data == "channel:add"
            else "Введите часть имени или названия канала.",
            kb(home_row()),
        )

    @router.callback_query(F.data == "channel:reset")
    async def channel_reset(c, state):
        await state.update_data(channel_search="")
        await channels(c, state)

    async def channel_card(event, id_):
        await require_channels(s.settings, s.db, event.from_user.id, [id_])
        async with s.db.session() as session:
            row = await session.get(Channel, id_)
        if not row or row.archived_at:
            raise ValueError("Канал не найден или в архиве")
        availability = await s.subscriptions.availability(id_)
        buttons = [
            [("Запустить", f"work:channel:{id_}", 16), ("Расписание", f"schedule:new:{id_}", 21)]
        ]
        if owner(s, event.from_user.id):
            buttons += [
                [
                    (
                        "Пауза 24/7" if row.permanent and row.active else "Включить 24/7",
                        f"own:channel:toggle:{id_}",
                        108,
                    )
                ],
                [
                    ("Подписать", f"own:subscribe:{id_}", 59),
                    ("Отписать", f"own:unsubscribe:{id_}", 33),
                ],
                [
                    ("Сверить подписки", f"own:sync:{id_}", 63),
                    ("В архив", f"own:channel:archive:{id_}", 33),
                ],
            ]
        await show(
            event,
            f"<b>{escape(row.title or '@' + row.username)}</b>\n{escape(row.url)}\n"
            f"24/7: {'включён' if row.permanent and row.active else 'на паузе'}\n"
            f"Проверка: каждые {row.check_interval_seconds // 60} мин.\n"
            f"ID Telegram: {row.telegram_id or 'определится при первом запуске'}\n"
            f"Подписано по локальным данным: {availability.subscribed_accounts}. Активных аккаунтов: {availability.active_accounts}.\n"
            "При общей паузе автоматические запуски остановлены.",
            kb(*buttons, [("К каналам", "channels:0", 67)], home_row()),
        )

    @router.callback_query(F.data.regexp(r"^channel:\d+$"))
    async def card_callback(c):
        await channel_card(c, int(c.data.split(":")[1]))

    @router.callback_query(F.data.startswith("own:channel:"))
    async def channel_action(c, state):
        _, _, action, id_text = c.data.split(":")
        id_ = int(id_text)
        if action == "archive":
            await show(
                c,
                "Канал будет исключён из новых запусков, его расписания отключены. Активные задания с этим каналом будут остановлены целиком. История останется.",
                kb(
                    [("Подтвердить архив", f"own:channel:delete:{id_}", 33)],
                    [("Назад", f"channel:{id_}", 67)],
                ),
            )
            return
        async with s.db.session() as session:
            channel = await session.get(Channel, id_)
            if not channel or channel.archived_at:
                raise ValueError("Канал не найден")
            if action == "toggle":
                enabled = not (channel.permanent and channel.active)
                channel.permanent, channel.active = True, enabled
                channel.last_cycle_at = None
                channel.created_by = c.from_user.id
            elif action == "delete":
                enabled = False
                channel.active, channel.archived_at = False, utcnow()
                await session.execute(
                    update(Schedule).where(Schedule.channel_id == id_).values(enabled=False)
                )
            else:
                raise ValueError("Неизвестное действие")
            query = (
                select(Job.id)
                .join(JobChannel)
                .where(
                    JobChannel.channel_id == id_,
                    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                )
            )
            # A per-channel pause must not cancel the other channels of a multi-channel job.
            # Workers check Channel.active before every Telegram view batch.
            ids = list((await session.scalars(query)).all()) if action == "delete" else []
        for job_id in ids:
            await s.runner.stop_job(job_id, c.from_user.id)
        await audit(s.db, c.from_user.id, "channel." + action, id_, enabled=enabled)
        if action == "delete":
            await channels(c, state)
        else:
            await channel_card(c, id_)

    @router.callback_query(F.data.regexp(r"^own:(subscribe|unsubscribe|sync):\d+$"))
    async def subscribe(c, state):
        _, action, id_ = c.data.split(":")
        if action == "sync":
            await show(
                c,
                "Сверка подписок запущена. Результат придёт отдельным сообщением.",
                kb([("Канал", f"channel:{id_}")]),
            )
            s.spawn(run_subscriptions(c.message, int(id_), action, 0, c.from_user.id), c.message)
        else:
            a = await s.subscriptions.availability(int(id_))
            available = (
                a.available_to_subscribe if action == "subscribe" else a.available_to_unsubscribe
            )
            await state.set_state(Input.text)
            await state.update_data(
                action="subscription", subscription_action=action, channel_id=int(id_)
            )
            await show(
                c,
                f"Сколько случайно выбранных аккаунтов использовать? Доступно: {available}.\nВведите число; перед выполнением будет подтверждение.",
                kb(home_row()),
            )

    async def run_subscriptions(message, channel_id, action, count, actor):
        from aiogram.types import FSInputFile

        result = await s.subscriptions.run(channel_id, action, count, actor)
        path = await s.reports.subscription_report(result)
        await message.answer_document(
            FSInputFile(path),
            caption=f"Готово: {len(result.succeeded)}/{result.requested}. Ошибок: {len(result.failed)}.",
            protect_content=True,
        )

    @router.callback_query(F.data.startswith("own:subconfirm:"))
    async def subconfirm(c, state):
        data = await state.get_data()
        if (
            data.get("token") != c.data.rsplit(":", 1)[1]
            or data.get("action") != "subscription_confirm"
        ):
            raise ValueError("Подтверждение устарело")
        await state.clear()
        await show(c, "Операция запущена. Подробный результат придёт файлом.", kb(home_row()))
        s.spawn(
            run_subscriptions(
                c.message,
                data["channel_id"],
                data["subscription_action"],
                data["count"],
                c.from_user.id,
            ),
            c.message,
        )

    async def work_menu(event, state, channel_id=None):
        await state.clear()
        await state.update_data(selected=[channel_id] if channel_id else [])
        await show(
            event,
            "<b>Что запускаем?</b>\nРазово — все публикации.\n24/7 — новые посты. Тест — на заданное время.",
            kb(
                [("Разовый проход", "work:mode:manual", 75, "primary")],
                [("Следить 24/7", "work:mode:permanent", 108, "primary")],
                [("Тест", "work:mode:test", 74)],
                home_row(),
            ),
        )

    @router.callback_query(F.data == "menu:work")
    async def work_home(c, state):
        await work_menu(c, state)

    @router.callback_query(F.data.startswith("work:channel:"))
    async def work_channel(c, state):
        await work_menu(c, state, int(c.data.rsplit(":", 1)[1]))

    async def selection(event, state, page=0):
        data = await state.get_data()
        selected = data.get("selected", [])
        scope = await channel_scope(s.settings, s.db, event.from_user.id)
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Channel)
                        .where(Channel.archived_at.is_(None))
                        .where(Channel.id.in_(scope) if scope is not None else True)
                        .order_by(Channel.id)
                        .offset(max(0, page) * PAGE)
                        .limit(PAGE + 1)
                    )
                ).all()
            )
        await show(
            event,
            f"<b>Выберите каналы</b> · {MODE_LABELS[data['mode']]}\nВыбрано: {len(selected)}. Лимит: {s.settings.max_channels_per_job}.",
            kb(
                *[
                    [
                        (
                            ("✓ " if r.id in selected else "") + "@" + r.username,
                            f"work:pick:{r.id}:{page}",
                            25,
                            "success" if r.id in selected else None,
                        )
                    ]
                    for r in rows[:PAGE]
                ],
                pages("work:page", page, len(rows) > PAGE),
                [("Продолжить", "work:next", 26)],
                [("Добавить ссылки", "work:links", 59), ("Сбросить выбор", "work:clear", 33)],
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("work:mode:"))
    async def work_mode(c, state):
        mode = c.data.rsplit(":", 1)[1]
        if mode not in MODES:
            raise ValueError("Неизвестный режим")
        await state.set_state(Input.selection)
        await state.update_data(mode=mode, action="work_selection")
        await selection(c, state)

    @router.callback_query(F.data.startswith("work:pick:"))
    async def pick(c, state):
        data = await state.get_data()
        if data.get("action") != "work_selection":
            raise ValueError("Начните новый запуск из меню")
        _, _, id_, page = c.data.split(":")
        selected = set(data.get("selected", []))
        id_ = int(id_)
        if id_ in selected:
            selected.remove(id_)
        elif len(selected) < s.settings.max_channels_per_job:
            selected.add(id_)
        else:
            raise ValueError("Достигнут лимит каналов в задании")
        await state.update_data(selected=sorted(selected))
        await selection(c, state, int(page))

    @router.callback_query(F.data.startswith("work:page:"))
    async def work_page(c, state):
        if (await state.get_data()).get("action") != "work_selection":
            raise ValueError("Выбор каналов устарел")
        await selection(c, state, int(c.data.rsplit(":", 1)[1]))

    @router.callback_query(F.data == "work:clear")
    async def clear_selection(c, state):
        await state.update_data(selected=[])
        await selection(c, state)

    @router.callback_query(F.data == "work:links")
    async def work_links(c, state):
        if (await state.get_data()).get("action") != "work_selection":
            raise ValueError("Начните новый запуск")
        await state.set_state(Input.text)
        await state.update_data(action="work_links")
        await show(
            c,
            "Пришлите публичные ссылки или @username. Они будут добавлены в текущий выбор.",
            kb(home_row()),
        )

    async def confirmation(event, state):
        data = await state.get_data()
        ids = data.get("selected", [])
        if not ids:
            raise ValueError("Выберите хотя бы один канал")
        if await paused(s.db):
            raise ValueError(
                "Включена общая пауза. Главный администратор может возобновить работу в меню."
            )
        async with s.db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(TelegramAccount)
                .where(
                    TelegramAccount.status == AccountStatus.ACTIVE,
                    TelegramAccount.archived_at.is_(None),
                )
            )
        token = secrets.token_hex(6)
        await state.set_state(Input.confirmation)
        await state.update_data(action="job_confirm", token=token)
        await show(
            event,
            f"<b>Подтвердите запуск</b>\nРежим: {MODE_LABELS[data['mode']]}.\nКаналов: {len(ids)}. Активных аккаунтов: {count}.\n"
            + (f"Длительность: {data.get('duration')} мин.\n" if data["mode"] == "test" else "")
            + "",
            kb([("Запустить", f"work:confirm:{token}", 16)], [("Отмена", "menu:home", 67)]),
        )

    @router.callback_query(F.data == "work:next")
    async def work_next(c, state):
        data = await state.get_data()
        if data.get("action") != "work_selection" or not data.get("selected"):
            raise ValueError("Сначала выберите каналы")
        if data["mode"] == "test":
            await state.set_state(Input.text)
            await state.update_data(action="test_duration")
            await show(
                c,
                "<b>Длительность теста</b>\nВыберите или введите минуты (1–10080).",
                kb(
                    [
                        ("5 мин", "work:duration:5", 74),
                        ("10 мин", "work:duration:10", 74),
                        ("30 мин", "work:duration:30", 74),
                    ],
                    home_row(),
                ),
            )
        else:
            await confirmation(c, state)

    @router.callback_query(F.data.startswith("work:duration:"))
    async def duration_preset(c, state):
        data = await state.get_data()
        if data.get("action") != "test_duration":
            raise ValueError("Начните новый тест")
        value = int(c.data.rsplit(":", 1)[1])
        if value not in (5, 10, 30):
            raise ValueError("Неизвестная длительность")
        await state.update_data(duration=value)
        await confirmation(c, state)

    @router.callback_query(F.data.startswith("work:confirm:"))
    async def work_confirm(c, state):
        data = await state.get_data()
        if data.get("action") != "job_confirm" or data.get("token") != c.data.rsplit(":", 1)[1]:
            raise ValueError("Подтверждение устарело или задание уже создано")
        job = await s.runner.create_from_channel_ids(
            data["selected"],
            mode=MODES[data["mode"]],
            created_by=c.from_user.id,
            duration_minutes=data.get("duration"),
        )
        await state.clear()
        s.runner.launch(job.id)
        await show(
            c,
            f"Задание <code>{job.id[:8]}</code> поставлено в очередь.",
            kb([("Прогресс и отчёт", f"job:{job.id}", 30)], home_row()),
        )

    @router.message(Command("view", "test", "permanent"))
    async def command_run(message, state):
        parts = message.text.split(maxsplit=1)
        command = parts[0].split("@")[0][1:]
        mode = {"view": "manual", "test": "test", "permanent": "permanent"}[command]
        if len(parts) < 2:
            await work_menu(message, state)
            return
        text = parts[1]
        duration = None
        if command == "test":
            duration_text, text = text.split(maxsplit=1)
            duration = int(duration_text)
            if not 1 <= duration <= 10080:
                raise ValueError("Длительность теста: 1–10080 минут")
        links = parse_channel_links(text)
        if len(links) > s.settings.max_channels_per_job:
            raise ValueError("Слишком много каналов")
        ids = await add_links(links, message.from_user.id)
        await state.clear()
        await state.update_data(mode=mode, selected=ids, duration=duration)
        await confirmation(message, state)

    async def schedules(event, page=0):
        scope = await channel_scope(s.settings, s.db, event.from_user.id)
        async with s.db.session() as session:
            rows = (
                await session.execute(
                    select(Schedule, Channel)
                    .join(Channel)
                    .where(Channel.archived_at.is_(None))
                    .where(Channel.id.in_(scope) if scope is not None else True)
                    .order_by(Schedule.id.desc())
                    .offset(max(0, page) * PAGE)
                    .limit(PAGE + 1)
                )
            ).all()
        await show(
            event,
            "<b>Расписания</b>\nСоздавайте расписание в карточке канала. Общая пауза блокирует все автоматические запуски.",
            kb(
                *[
                    [
                        (
                            f"{'▶' if r.enabled else 'Ⅱ'} @{c.username} · {date(s, r.run_at)}",
                            f"schedule:{r.id}",
                            21,
                        )
                    ]
                    for r, c in rows[:PAGE]
                ],
                pages("schedules", page, len(rows) > PAGE),
                [("Выбрать канал", "channels:0", 25)],
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("schedules:"))
    async def schedule_list(c):
        await schedules(c, int(c.data.rsplit(":", 1)[1]))

    async def schedule_card(event, id_):
        async with s.db.session() as session:
            row = await session.get(Schedule, id_)
            channel = await session.get(Channel, row.channel_id) if row else None
        if not row or not channel:
            raise ValueError("Расписание не найдено")
        await require_channels(s.settings, s.db, event.from_user.id, [row.channel_id])
        buttons = []
        if owner(s, event.from_user.id) or row.created_by == event.from_user.id:
            buttons = [
                [("Приостановить" if row.enabled else "Возобновить", f"schedule:toggle:{id_}", 63)],
                [
                    ("Изменить", f"schedule:edit:{id_}", 34),
                    ("Удалить", f"schedule:delete:{id_}", 33),
                ],
            ]
        await show(
            event,
            f"<b>Расписание №{id_}</b> · @{escape(channel.username)}\n"
            f"{'Работает' if row.enabled else 'На паузе'}\nСледующий запуск: {date(s, row.run_at)} ({s.settings.timezone})\n"
            f"Интервал: {str(row.interval_minutes) + ' мин.' if row.kind == ScheduleKind.INTERVAL else 'один раз'}\n"
            f"Последний запуск: {date(s, row.last_run_at)}\nПричина ожидания: {escape(row.last_error or 'нет')}",
            kb(
                *buttons,
                *([[("Последний отчёт", f"job:{row.last_job_id}", 30)]] if row.last_job_id else []),
                [("Расписания", "schedules:0", 67)],
            ),
        )

    @router.callback_query(F.data.regexp(r"^schedule:\d+$"))
    async def schedule_show(c):
        await schedule_card(c, int(c.data.split(":")[1]))

    async def schedule_prompt(event, state, channel_id, schedule_id=None):
        await require_channels(s.settings, s.db, event.from_user.id, [channel_id])
        await state.set_state(Input.text)
        await state.update_data(action="schedule", channel_id=channel_id, schedule_id=schedule_id)
        await show(
            event,
            f"Укажите дату в часовом поясе {s.settings.timezone}: <code>2026-12-25 18:30</code>\n"
            "Или интервал в минутах: <code>каждые 120</code>. Минимум — 60 минут. Разовый запуск — в пределах года.",
            kb(home_row()),
        )

    @router.callback_query(F.data.regexp(r"^schedule:(new|toggle|edit|delete|confirm):\d+$"))
    async def schedule_action(c, state):
        _, action, text_id = c.data.split(":")
        id_ = int(text_id)
        if action == "new":
            await schedule_prompt(c, state, id_)
            return
        async with s.db.session() as session:
            row = await session.get(Schedule, id_)
        if not row:
            raise ValueError("Расписание удалено")
        await require_channels(s.settings, s.db, c.from_user.id, [row.channel_id])
        if not owner(s, c.from_user.id) and row.created_by != c.from_user.id:
            raise ValueError("Изменить расписание может его автор или главный администратор")
        if action == "toggle":
            await s.schedules.toggle(id_, c.from_user.id)
            await schedule_card(c, id_)
        elif action == "edit":
            await schedule_prompt(c, state, row.channel_id, id_)
        elif action == "delete":
            await show(
                c,
                "Удалить это расписание?",
                kb(
                    [("Удалить", f"schedule:confirm:{id_}", 33)],
                    [("Отмена", f"schedule:{id_}", 67)],
                ),
            )
        else:
            await s.schedules.delete(id_, c.from_user.id)
            await schedules(c)

    async def text_input(message, state, data):
        action = data.get("action")
        if action in ("channel_add", "work_links"):
            links = parse_channel_links(message.text)
            if len(links) > s.settings.max_channels_per_job:
                raise ValueError("Слишком много ссылок за один раз")
            ids = await add_links(links, message.from_user.id)
            await audit(s.db, message.from_user.id, "channels.added", count=len(ids))
            if action == "work_links":
                selected = list(dict.fromkeys(data.get("selected", []) + ids))
                if len(selected) > s.settings.max_channels_per_job:
                    raise ValueError("Слишком много каналов в выборе")
                await state.set_state(Input.selection)
                await state.update_data(selected=selected, action="work_selection")
                await selection(message, state)
            else:
                await state.clear()
                await channels(message, state)
        elif action == "channel_search":
            await state.update_data(channel_search=message.text.strip()[:64])
            await state.set_state(None)
            await channels(message, state)
        elif action == "test_duration":
            duration = int(message.text.strip())
            if not 1 <= duration <= 10080:
                raise ValueError("Длительность: 1–10080 минут")
            await state.update_data(duration=duration)
            await confirmation(message, state)
        elif action == "subscription":
            require_owner(s, message.from_user.id)
            count = int(message.text.strip())
            a = await s.subscriptions.availability(data["channel_id"])
            available = (
                a.available_to_subscribe
                if data["subscription_action"] == "subscribe"
                else a.available_to_unsubscribe
            )
            if not 1 <= count <= available:
                raise ValueError(f"Введите число от 1 до {available}")
            token = secrets.token_hex(6)
            await state.set_state(Input.confirmation)
            await state.update_data(action="subscription_confirm", count=count, token=token)
            label = "Подписать" if data["subscription_action"] == "subscribe" else "Отписать"
            await message.answer(
                f"{label} {count} случайно выбранных аккаунтов?",
                reply_markup=kb([(label, f"own:subconfirm:{token}", 26)], home_row()),
            )
        elif action == "schedule":
            text = message.text.strip()
            interval, run_at = None, None
            if text.lower().startswith("каждые "):
                interval = int(text.split(maxsplit=1)[1])
            else:
                run_at = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=s.settings.tz)
            if data.get("schedule_id"):
                async with s.db.session() as session:
                    row = await session.get(Schedule, data["schedule_id"])
                if not row or (
                    not owner(s, message.from_user.id) and row.created_by != message.from_user.id
                ):
                    raise ValueError("Нет доступа к этому расписанию")
            await require_channels(s.settings, s.db, message.from_user.id, [data["channel_id"]])
            row = await s.schedules.save(
                data["channel_id"],
                message.from_user.id,
                interval=interval,
                run_at=run_at,
                schedule_id=data.get("schedule_id"),
            )
            await state.clear()
            await schedule_card(message, row.id)
        else:
            return False
        return True

    return router, text_input
