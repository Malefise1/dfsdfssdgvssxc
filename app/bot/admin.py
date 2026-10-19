from __future__ import annotations

import secrets
import shutil
import time
from html import escape

from aiogram import F, Router
from sqlalchemy import func, select

from app.bot.common import (
    PAGE,
    Input,
    date,
    download_secret,
    home_row,
    kb,
    pages,
    require_owner,
    show,
)
from app.enums import JobStatus, OperatorRole, ProxyProtocol
from app.models import (
    AuditEvent,
    Channel,
    Job,
    JobChannel,
    KnownUser,
    Operator,
    Proxy,
    TelegramAccount,
)
from app.repositories import (
    list_manager_candidates,
    list_operators,
    promote_manager,
    remove_manager,
)
from app.services.control import audit, put_setting, setting
from app.services.proxies import parse_proxy_file


def create_router(s):
    router = Router(name="admin")

    async def proxies(event, page=0):
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Proxy).order_by(Proxy.id).offset(max(0, page) * PAGE).limit(PAGE + 1)
                    )
                ).all()
            )
        policy = await setting(s.db, "proxy_policy", {"value": s.settings.proxy_policy})
        await show(
            event,
            "<b>Прокси</b>\nПолитика: "
            + ("обязательны" if policy["value"] == "required" else "прямое подключение разрешено")
            + "\n🟢 Проверен · 🔴 Недоступен · ⚪ Не проверен\nНазначение: Аккаунты → аккаунт → Прокси.",
            kb(
                [
                    ("Добавить SOCKS5", "own:proxy:upload:socks5", 59),
                    ("Добавить HTTP", "own:proxy:upload:http", 59),
                ],
                [("Проверить все", "own:proxy:check", 123)],
                [("Удалить выбранные", "own:pdel:select", 33), ("Удалить все", "own:pdel:all", 33)],
                *[
                    [
                        (
                            f"{'⏸' if not r.active else '🟢' if r.last_check_ok is True else '🔴' if r.last_check_ok is False else '⚪'} #{r.id} {r.protocol.value} {r.host}:{r.port}",
                            f"own:proxy:card:{r.id}",
                            83,
                        )
                    ]
                    for r in rows[:PAGE]
                ],
                pages("own:proxies", page, len(rows) > PAGE),
                [("Политика подключения", "own:settings", 104)],
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("own:proxies:"))
    async def proxy_list(c, state):
        await state.clear()
        await proxies(c, int(c.data.rsplit(":", 1)[1]))

    async def delete_selection(c, state, token, page=0):
        data = await state.get_data()
        if data.get("proxy_delete_select") != token or time.monotonic() > data.get(
            "proxy_select_until", 0
        ):
            raise ValueError("Выбор устарел. Откройте «Прокси → Удалить выбранные».")
        page = max(0, page)
        selected = set(data.get("proxy_delete_ids", []))
        async with s.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(Proxy))
            page = min(page, max(0, (total - 1) // PAGE))
            rows = list(
                (
                    await session.scalars(
                        select(Proxy).order_by(Proxy.id).offset(page * PAGE).limit(PAGE + 1)
                    )
                ).all()
            )
            selected = set(
                (await session.scalars(select(Proxy.id).where(Proxy.id.in_(selected)))).all()
            )
        await state.set_state(Input.selection)
        await state.update_data(proxy_delete_ids=sorted(selected), proxy_delete_confirm=None)
        await show(
            c,
            f"<b>Удаление прокси</b> · выбрано {len(selected)}\nОтметьте нужные кнопками.",
            kb(
                *[
                    [
                        (
                            f"{'✅' if row.id in selected else '⬜'} #{row.id} {row.host}:{row.port}",
                            f"own:pdel:toggle:{token}:{row.id}:{page}",
                            83,
                            "primary" if row.id in selected else None,
                        )
                    ]
                    for row in rows[:PAGE]
                ],
                [
                    ("Выбрать страницу", f"own:pdel:pageall:{token}:{page}"),
                    ("Снять выбор", f"own:pdel:clear:{token}:{page}"),
                ],
                pages(f"own:pdel:page:{token}", page, len(rows) > PAGE),
                [(f"Удалить выбранные · {len(selected)}", f"own:pdel:review:{token}", 33)]
                if selected
                else [],
                [("Отмена", "own:proxies:0", 67)],
            ),
        )

    async def delete_preview(c, state, ids):
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(select(Proxy).where(Proxy.id.in_(ids)).order_by(Proxy.id))
                ).all()
            )
            ids = [row.id for row in rows]
            accounts = await session.scalar(
                select(func.count())
                .select_from(TelegramAccount)
                .where(
                    TelegramAccount.last_proxy_id.in_(ids),
                    TelegramAccount.archived_at.is_(None),
                )
            )
        if not ids:
            await state.clear()
            await show(
                c, "Прокси уже удалены или список пуст.", kb([("Прокси", "own:proxies:0", 67)])
            )
            return
        token = secrets.token_hex(6)
        await state.set_state(Input.confirmation)
        await state.update_data(
            proxy_delete_confirm=token,
            proxy_confirm_ids=ids,
            proxy_confirm_until=time.monotonic() + 300,
        )
        names = "\n".join(f"#{r.id} {escape(r.host)}:{r.port}" for r in rows[:5])
        if len(rows) > 5:
            names += f"\n…и ещё {len(rows) - 5}"
        await show(
            c,
            f"<b>Удалить прокси: {len(ids)}?</b>\n{names}\n\n"
            f"Привязанных аккаунтов: {accounts}. Активные будут на паузе.\n"
            "Сессии сохранятся. Для продолжения назначьте новые прокси и включите аккаунты.",
            kb(
                [("Подтвердить удаление", f"own:pdel:apply:{token}", 33)],
                [("Отмена", "own:proxies:0", 67)],
            ),
        )

    @router.callback_query(F.data.startswith("own:pdel:"))
    async def proxy_delete(c, state):
        require_owner(s, c.from_user.id)
        parts = c.data.split(":")
        action = parts[2]
        if action == "select":
            await state.clear()
            token = secrets.token_hex(6)
            await state.update_data(
                proxy_delete_select=token,
                proxy_delete_ids=[],
                proxy_select_until=time.monotonic() + 1800,
            )
            await delete_selection(c, state, token)
        elif action in ("all", "one"):
            await state.clear()
            async with s.db.session() as session:
                query = select(Proxy.id)
                if action == "one":
                    query = query.where(Proxy.id == int(parts[3]))
                ids = list((await session.scalars(query)).all())
            await delete_preview(c, state, ids)
        elif action == "apply":
            data = await state.get_data()
            if (
                await state.get_state() != Input.confirmation.state
                or data.get("proxy_delete_confirm") != parts[3]
                or time.monotonic() > data.get("proxy_confirm_until", 0)
            ):
                raise ValueError("Подтверждение устарело. Выберите прокси заново.")
            if s.onboarding.active:
                raise ValueError("Сначала завершите добавление аккаунтов")
            ids = data.get("proxy_confirm_ids", [])
            await state.clear()  # A repeated click must never perform a second deletion.
            await show(c, "Удаляю прокси…")
            result = await s.proxies.delete_many(ids, s.runtime, c.from_user.id)
            await show(
                c,
                f"Удалено прокси: {result['deleted']}.\nПоставлено аккаунтов на паузу: {result['paused']}.\nСессии сохранены.",
                kb([("Прокси", "own:proxies:0", 28), ("Аккаунты", "accounts:all:0", 3)]),
            )
        else:
            data = await state.get_data()
            token = parts[3]
            if data.get("proxy_delete_select") != token or time.monotonic() > data.get(
                "proxy_select_until", 0
            ):
                raise ValueError("Выбор устарел. Откройте «Прокси → Удалить выбранные».")
            selected = set(data.get("proxy_delete_ids", []))
            if action == "review":
                await delete_preview(c, state, sorted(selected))
                return
            page = int(parts[-1])
            if action == "toggle":
                id_ = int(parts[4])
                async with s.db.session() as session:
                    if not await session.get(Proxy, id_):
                        raise ValueError("Прокси уже удалён")
                selected.symmetric_difference_update({id_})
            elif action == "pageall":
                async with s.db.session() as session:
                    selected.update(
                        (
                            await session.scalars(
                                select(Proxy.id)
                                .order_by(Proxy.id)
                                .offset(max(0, page) * PAGE)
                                .limit(PAGE)
                            )
                        ).all()
                    )
            elif action == "clear":
                selected.clear()
            elif action != "page":
                raise ValueError("Неизвестное действие")
            await state.update_data(proxy_delete_ids=sorted(selected))
            await delete_selection(c, state, token, page)

    @router.callback_query(F.data.startswith("own:proxy:upload:"))
    async def proxy_upload(c, state):
        protocol = c.data.rsplit(":", 1)[1]
        if protocol not in ("http", "socks5"):
            raise ValueError("Неизвестный протокол")
        await state.set_state(Input.file)
        await state.update_data(action="proxy_upload", protocol=protocol)
        await show(
            c,
            f"<b>Добавить {protocol.upper()}</b>\nТекстом или TXT, по одному на строку:\n"
            "<code>login:password@host:port</code>\n<code>host:port:login:password</code>\n"
            "<code>host:port</code> — без пароля\n"
            "Ссылки socks5:// и http:// задают свой протокол.\nСообщение с реквизитами будет удалено.",
            kb([("Назад", "own:proxies:0", 67)]),
        )

    @router.callback_query(F.data == "own:proxy:check")
    async def proxy_check(c):
        async def check():
            ok, total = await s.proxies.check_all()
            await c.message.answer(
                f"{'🟢' if ok == total and total else '🔴'} Telegram: {ok}/{total} прокси работают."
                + ("\nПодробности — в карточках прокси." if ok < total else ""),
                reply_markup=kb([("Прокси", "own:proxies:0")]),
            )
            await audit(s.db, c.from_user.id, "proxies.checked", ok=ok, total=total)

        s.spawn(check(), c.message)
        await show(c, "Проверяю соединение с Telegram…", kb([("Прокси", "own:proxies:0", 28)]))

    @router.callback_query(F.data.regexp(r"^own:proxy:(card|toggle|enable|disable|test):\d+$"))
    async def proxy_card(c):
        _, _, action, id_ = c.data.split(":")
        id_ = int(id_)
        async with s.db.session() as session:
            row = await session.get(Proxy, id_)
            if not row:
                raise ValueError("Прокси не найден")
            changed = action in ("enable", "disable") and row.active != (action == "enable")
            if changed:
                row.active = action == "enable"
                row.last_check_ok = None
                row.last_checked_at = None
                row.last_error = None
            accounts = list(
                (
                    await session.scalars(
                        select(TelegramAccount.id).where(
                            TelegramAccount.last_proxy_id == id_,
                            TelegramAccount.archived_at.is_(None),
                        )
                    )
                ).all()
            )
        if action == "toggle":
            await show(
                c,
                ("Отключить" if row.active else "Включить")
                + f" прокси #{id_}?\n"
                + f"Закреплено аккаунтов: {len(accounts)}. Их текущие подключения будут остановлены; сессии сохранятся.",
                kb(
                    [
                        (
                            "Отключить" if row.active else "Включить",
                            f"own:proxy:{'disable' if row.active else 'enable'}:{id_}",
                            63,
                        )
                    ],
                    [("Отмена", f"own:proxy:card:{id_}", 67)],
                ),
            )
            return
        if action == "test":
            if not row.active:
                raise ValueError("Сначала включите прокси")

            async def check_one():
                ok = await s.proxies.check_proxy(row)
                await c.message.answer(
                    f"Прокси #{id_}: {'🟢 Telegram доступен' if ok else '🔴 Проверка не пройдена'}",
                    reply_markup=kb([("Открыть", f"own:proxy:card:{id_}", 28)]),
                )

            s.spawn(check_one(), c.message)
            await show(
                c, "Проверяю соединение с Telegram…", kb([("Назад", f"own:proxy:card:{id_}", 67)])
            )
            return
        if changed:
            for account_id in accounts:
                await s.runtime.cancel_account(account_id)
            await audit(s.db, c.from_user.id, "proxy.active", id_, active=row.active)
        await show(
            c,
            f"<b>Прокси #{row.id}</b>\n{escape(row.host)}:{row.port} · {row.protocol.value}\n"
            f"Включён: {'да' if row.active else 'нет'}\nЗакреплено аккаунтов: {len(accounts)}\n"
            f"Telegram: {'🟢 доступен' if row.last_check_ok else '🔴 недоступен' if row.last_check_ok is False else '⚪ не проверен'}\n"
            f"Проверен: {date(s, row.last_checked_at)}"
            + (f"\n{escape(row.last_error)}" if row.last_error else ""),
            kb(
                [("Проверить", f"own:proxy:test:{id_}", 123)],
                [("Отключить" if row.active else "Включить", f"own:proxy:toggle:{id_}", 63)],
                [("Удалить", f"own:pdel:one:{id_}", 33)],
                [("Прокси", "own:proxies:0", 67)],
            ),
        )

    @router.callback_query(F.data == "own:operators")
    async def operators(c):
        async with s.db.session() as session:
            rows = await list_operators(session)
            candidates = await list_manager_candidates(session)
        buttons = [
            [(f"Убрать менеджера {r.telegram_id}", f"own:operator:remove:{r.telegram_id}", 33)]
            for r in rows
            if r.role == OperatorRole.MANAGER
        ]
        buttons += [
            [(f"Каналы менеджера {row.telegram_id}", f"own:scope:{row.telegram_id}", 25)]
            for row in rows
            if row.role == OperatorRole.MANAGER
        ]
        buttons += [
            [
                (
                    f"Добавить {r.full_name[:30]} · {r.telegram_id}",
                    f"own:operator:add:{r.telegram_id}",
                    59,
                )
            ]
            for r in candidates[:20]
        ]
        await show(
            c,
            "<b>Управляющие</b>\nГлавный администратор задаётся в конфигурации. Менеджер сначала отправляет /start боту, затем появляется в этом списке.\n"
            "Менеджеры видят аккаунты, запускают задания, создают расписания и скачивают отчёты. Изменение аккаунтов, прокси и доступов — только у владельца.",
            kb(*buttons, home_row()),
        )

    @router.callback_query(F.data.startswith("own:scope:"))
    async def manager_scope(c, state):
        manager = int(c.data.rsplit(":", 1)[1])
        async with s.db.session() as session:
            row = await session.get(Operator, manager)
        if not row or row.role != OperatorRole.MANAGER:
            raise ValueError("Менеджер не найден")
        await state.set_state(Input.text)
        await state.update_data(action="manager_scope", manager=manager)
        await show(
            c,
            "Введите разрешённые @username через пробел; «все» снимает ограничение, «нет» закрывает доступ к каналам. "
            "Ограничение действует на каналы, новые задания, расписания и отчёты. Старые скачанные файлы оно не удаляет.",
            kb(home_row()),
        )

    async def text_input(message, state, data):
        if data.get("action") == "proxy_upload":
            require_owner(s, message.from_user.id)
            try:
                await message.delete()
            except Exception as exc:
                raise ValueError(
                    "Не удалось удалить реквизиты. Удалите сообщение и повторите."
                ) from exc
            await import_proxies(message, state, data, message.text)
            return True
        if data.get("action") != "manager_scope":
            return False
        require_owner(s, message.from_user.id)
        value = message.text.strip()
        ids = None
        if value.lower() == "нет":
            ids = []
        elif value.lower() != "все":
            from app.telegram.links import parse_channel_links

            links = parse_channel_links(value)
            async with s.db.session() as session:
                rows = list(
                    (
                        await session.scalars(
                            select(Channel).where(
                                Channel.username.in_([x.username.lower() for x in links]),
                                Channel.archived_at.is_(None),
                            )
                        )
                    ).all()
                )
            if len(rows) != len(links):
                raise ValueError("Сначала добавьте все эти каналы в общий список")
            ids = [row.id for row in rows]
        await put_setting(s.db, f"manager_channels:{data['manager']}", {"ids": ids})
        if ids is not None:
            async with s.db.session() as session:
                job_ids = list(
                    (
                        await session.scalars(
                            select(Job.id).where(
                                Job.created_by == data["manager"],
                                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                                Job.id.in_(
                                    select(JobChannel.job_id).where(
                                        JobChannel.channel_id.not_in(ids)
                                    )
                                ),
                            )
                        )
                    ).all()
                )
            for job_id in job_ids:
                await s.runner.stop_job(job_id, message.from_user.id)
        await audit(s.db, message.from_user.id, "manager.channel_scope", data["manager"], ids=ids)
        await state.clear()
        await message.answer(
            "Доступ к каналам обновлён.", reply_markup=kb([("Управляющие", "own:operators")])
        )
        return True

    @router.callback_query(F.data.startswith("own:operator:"))
    async def operator_action(c):
        _, _, action, id_ = c.data.split(":")
        id_ = int(id_)
        if id_ == s.settings.main_admin_id:
            raise ValueError("Владелец меняется только в конфигурации")
        if action in ("add", "remove"):
            await show(
                c,
                (
                    "Дать доступ к заданиям, аккаунтам и отчётам"
                    if action == "add"
                    else "Отозвать доступ"
                )
                + f" у пользователя {id_}?",
                kb(
                    [
                        (
                            "Подтвердить",
                            f"own:operator:{'grant' if action == 'add' else 'revoke'}:{id_}",
                            26,
                        )
                    ],
                    [("Отмена", "own:operators", 67)],
                ),
            )
            return
        async with s.db.session() as session:
            if action == "grant":
                if not await session.get(KnownUser, id_):
                    raise ValueError("Пользователь сначала должен отправить /start")
                await promote_manager(session, id_)
            elif action == "revoke":
                await remove_manager(session, id_)
            else:
                raise ValueError("Неизвестное действие")
        await audit(s.db, c.from_user.id, "operator." + action, id_)
        await show(c, "Доступ обновлён.", kb([("Управляющие", "own:operators")]))

    @router.callback_query(F.data == "own:settings")
    async def settings(c):
        policy = await setting(s.db, "proxy_policy", {"value": s.settings.proxy_policy})
        phones = await setting(
            s.db, "manager_show_phones", {"value": s.settings.manager_show_phones}
        )
        await show(
            c,
            f"<b>Настройки</b>\nПрокси: {'обязательны' if policy['value'] == 'required' else 'можно без прокси'}.\n"
            f"Телефоны менеджерам: {'полностью' if phones['value'] else 'частично скрыты'}.\n"
            "Если закреплённый прокси недоступен — прямого выхода не будет.",
            kb(
                [("Только через прокси", "own:policy:required", 28)],
                [("Разрешить прямое подключение", "own:policy:allow_direct", 68)],
                [
                    (
                        "Скрыть телефоны" if phones["value"] else "Показывать телефоны",
                        f"own:phones:{int(not phones['value'])}",
                        47,
                    )
                ],
                [("Состояние", "own:health", 82), ("Доступ", "own:operators", 32)],
                [("Справка", "menu:help", 17)],
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("own:policy:"))
    async def policy(c):
        value = c.data.rsplit(":", 1)[1]
        if value not in ("required", "allow_direct"):
            raise ValueError("Неизвестная политика")
        await show(
            c,
            (
                "Без доступного прокси новые подключения будут отклоняться."
                if value == "required"
                else "Без назначенного прокси разрешается выход через IP сервера. Назначенный неработающий прокси не обходится."
            )
            + "\nТекущие операции аккаунтов будут остановлены. Применить?",
            kb([("Применить", f"own:policyconfirm:{value}", 26)], [("Отмена", "own:settings", 67)]),
        )

    @router.callback_query(F.data.startswith("own:policyconfirm:"))
    async def apply_policy(c):
        value = c.data.rsplit(":", 1)[1]
        if value not in ("required", "allow_direct"):
            raise ValueError("Неизвестная политика")
        await put_setting(s.db, "proxy_policy", {"value": value})
        await s.runtime.stop_all()
        s.onboarding.cancel(c.from_user.id)
        await audit(s.db, c.from_user.id, "proxy.policy", value=value)
        await show(
            c,
            "Политика применена. Остановленные задания можно запустить снова.",
            kb([("Настройки", "own:settings")]),
        )

    @router.callback_query(F.data.startswith("own:phones:"))
    async def phones(c):
        value = c.data.rsplit(":", 1)[1] == "1"
        await put_setting(s.db, "manager_show_phones", {"value": value})
        await audit(s.db, c.from_user.id, "privacy.phones", value=value)
        await show(
            c,
            "Новые экраны и выгрузки используют выбранную настройку. Ранее скачанные файлы она не меняет.",
            kb([("Настройки", "own:settings")]),
        )

    @router.callback_query(F.data == "own:health")
    async def health(c):
        state = s.backups.state()
        free = shutil.disk_usage(s.settings.data_dir).free // (1024**2)
        checks = "\n".join(
            f"{escape(name)}: {'OK' if value['ok'] else 'ошибка'} · {escape(value.get('at', ''))}"
            for name, value in s.health.items()
        )
        await show(
            c,
            f"<b>Состояние · 2.0.2</b>\nПодключений: {len(s.runtime.activity)} / {s.settings.max_concurrent_accounts}\n"
            f"Заданий в памяти: {len(s.runner._tasks)}\nСвободно на диске: {free} МБ\n"
            f"Последняя копия: {escape(state.get('success_at', 'ещё нет'))}\n"
            f"Ошибка копирования: {escape(state.get('error', 'нет'))}\n{checks}\n"
            "",
            kb(
                [("Обновить", "own:health", 63), ("Сделать копию", "own:backup", 93)],
                [("Журнал действий", "own:audit:0", 30)],
                home_row(),
            ),
        )

    @router.callback_query(F.data == "own:backup")
    async def backup(c):
        async def run():
            path = await s.backups.create_backup()
            await audit(s.db, c.from_user.id, "backup.created", path.name)
            await c.message.answer(
                "Резервная копия создана и её каталог проверен. Файл сохранён на сервере.",
                reply_markup=kb([("Состояние", "own:health")]),
            )

        s.spawn(run(), c.message)
        await c.message.answer("Создаю резервную копию.")

    @router.callback_query(F.data.startswith("own:audit:"))
    async def audit_log(c):
        page = max(0, int(c.data.rsplit(":", 1)[1]))
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(AuditEvent)
                        .order_by(AuditEvent.id.desc())
                        .offset(page * PAGE)
                        .limit(PAGE + 1)
                    )
                ).all()
            )
        text = "\n\n".join(
            f"{date(s, r.created_at)} · {r.actor_id}\n{escape(r.action)} · {escape(r.target[:80])}"
            for r in rows[:PAGE]
        )
        await show(
            c,
            "<b>Журнал действий</b>\n" + text,
            kb(pages("own:audit", page, len(rows) > PAGE), home_row()),
        )

    async def file_input(message, state, data):
        if data.get("action") != "proxy_upload":
            return False
        require_owner(s, message.from_user.id)
        name, content = await download_secret(message, s.settings)
        if not name.lower().endswith(".txt"):
            raise ValueError("Нужен TXT-файл")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Файл должен быть в UTF-8") from exc
        content = b""
        await import_proxies(message, state, data, text)
        return True

    async def import_proxies(message, state, data, text):
        rows, errors = parse_proxy_file(text)
        if errors:
            reasons = "\n".join(f"Строка {line}: {escape(reason)}" for line, reason in errors[:15])
            await message.answer(
                f"Ошибок: {len(errors)}. Ничего не добавлено. Исправьте и отправьте снова.\n"
                + reasons
            )
            return True
        if not rows:
            raise ValueError("Файл пуст")
        added = await s.proxies.import_many(ProxyProtocol(data["protocol"]), rows)
        await audit(s.db, message.from_user.id, "proxies.imported", total=len(rows), added=added)
        await state.clear()
        await message.answer(
            f"Сохранено: {len(rows)}. Новых: {added}.\nПовторы не меняют назначения аккаунтов. Проверьте соединение.",
            reply_markup=kb([("Проверить", "own:proxy:check", 123)], [("Прокси", "own:proxies:0")]),
        )
        return True

    return router, file_input, text_input
