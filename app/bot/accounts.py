from __future__ import annotations

import asyncio
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.types import FSInputFile
from sqlalchemy import or_, select

from app.bot.common import (
    PAGE,
    Input,
    date,
    download_secret,
    hide_phones,
    home_row,
    kb,
    owner,
    pages,
    require_owner,
    show,
)
from app.enums import AccountStatus
from app.models import (
    AccountChannelSubscription,
    Channel,
    ImportBatch,
    Proxy,
    TelegramAccount,
    utcnow,
)
from app.services.access import channel_scope
from app.services.control import audit, aware
from app.services.imports import ImportItem, parse_upload
from app.services.reports import masked_phone


def create_router(s):
    router = Router(name="accounts")

    async def listing(event, state, kind="all", page=0):
        page = max(0, min(page, 100000))
        data = await state.get_data()
        search = data.get("account_search", "")
        query = select(TelegramAccount).where(TelegramAccount.archived_at.is_(None))
        if kind in ("active", "disabled", "quarantined"):
            query = query.where(TelegramAccount.status == AccountStatus(kind))
        if kind == "problems":
            query = query.where(
                or_(TelegramAccount.last_error.is_not(None), TelegramAccount.health_ok.is_(False))
            )
        if search:
            clauses = [TelegramAccount.label.icontains(search, autoescape=True)]
            if not await hide_phones(s, event.from_user.id):
                clauses.append(TelegramAccount.phone.contains(search, autoescape=True))
            if search.isdigit():
                clauses.append(TelegramAccount.display_number == int(search))
            query = query.where(or_(*clauses))
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(TelegramAccount.display_number)
                        .offset(page * PAGE)
                        .limit(PAGE + 1)
                    )
                ).all()
            )
        mask = await hide_phones(s, event.from_user.id)
        buttons = [
            [
                (
                    f"{'🔴' if r.status == AccountStatus.QUARANTINED or r.health_ok is False else '⏸' if r.status == AccountStatus.DISABLED else '🟢'} №{r.display_number} · {r.label[:24] or (masked_phone(r.phone) if mask else r.phone)}",
                    f"account:{r.id}",
                    83,
                )
            ]
            for r in rows[:PAGE]
        ]
        if owner(s, event.from_user.id):
            buttons.insert(
                0,
                [
                    ("Добавить", "own:add", 59),
                    ("Проверить все", "own:accounts:check", 123),
                ],
            )
        buttons += [
            [
                ("Все", "accounts:all:0"),
                ("Активные", "accounts:active:0"),
                ("Проблемные", "accounts:problems:0"),
            ],
            [
                ("Остановленные", "accounts:disabled:0"),
                ("Найти", "account:search", 123),
                ("Сброс", "account:reset"),
            ],
            pages(f"accounts:{kind}", page, len(rows) > PAGE),
            home_row(),
        ]
        await show(
            event,
            f"<b>Аккаунты</b> · {page + 1}\n🟢 В работе · ⏸ Пауза · 🔴 Проверить"
            + (f"\nПоиск: {escape(search)}" if search else "")
            + ("" if rows else "\nАккаунтов по этому фильтру нет."),
            kb(*buttons),
        )

    @router.callback_query(F.data.startswith("accounts:"))
    async def list_callback(c, state):
        _, kind, page = c.data.split(":")
        await listing(c, state, kind, int(page))

    @router.callback_query(F.data == "account:search")
    async def search(c, state):
        await state.set_state(Input.text)
        await state.update_data(action="account_search")
        await show(c, "Введите номер аккаунта, часть имени или телефона.", kb(home_row()))

    @router.callback_query(F.data == "account:reset")
    async def reset(c, state):
        await state.update_data(account_search="")
        await listing(c, state)

    async def card(event, account_id):
        async with s.db.session() as session:
            row = await session.get(TelegramAccount, account_id)
            missing_proxy = bool(
                row and row.last_proxy_id and not await session.get(Proxy, row.last_proxy_id)
            )
        if not row or row.archived_at:
            raise ValueError("Аккаунт находится в архиве или не найден")
        value = masked_phone(row.phone) if await hide_phones(s, event.from_user.id) else row.phone
        status = {
            "active": "активен",
            "disabled": "остановлен",
            "quarantined": "требует авторизации",
        }[row.status.value]
        activity = s.runtime.activity.get(row.id, "свободен")
        if (
            await channel_scope(s.settings, s.db, event.from_user.id) is not None
            and row.id in s.runtime.activity
        ):
            activity = "аккаунт занят"
        text = (
            f"<b>Аккаунт №{row.display_number}</b>\n{escape(row.label)}\n{escape(value)}\n"
            f"{status.capitalize()} · {escape(activity)}\nПрокси: "
            + (
                f"удалён (#{row.last_proxy_id}) — назначьте новый"
                if missing_proxy
                else str(row.last_proxy_id or "автовыбор")
            )
            + "\n"
            f"Проверка: {date(s, row.health_checked_at)}"
            + (
                f"\nОжидание до: {date(s, row.flood_until)}"
                if row.flood_until and aware(row.flood_until) > utcnow()
                else ""
            )
            + (f"\n🔴 {escape(row.last_error[:350])}" if row.last_error else "")
        )
        buttons = []
        if owner(s, event.from_user.id):
            action = "disable" if row.status == AccountStatus.ACTIVE else "enable"
            buttons = [
                [
                    (
                        "Остановить" if action == "disable" else "Включить",
                        f"own:account:{action}:{row.id}",
                        125 if action == "disable" else 26,
                    ),
                    ("Проверить", f"own:account:check:{row.id}", 123),
                ],
                [
                    ("Имя", f"own:account:label:{row.id}", 118),
                    ("Прокси", f"own:account:proxy:{row.id}", 28),
                ],
                [
                    ("Войти заново", f"own:account:reauth:{row.id}", 63),
                    ("Удалить", f"own:account:archive:{row.id}", 33),
                ],
            ]
        buttons.append([("Подписки аккаунта", f"account:subscriptions:{account_id}:0", 25)])
        await show(event, text, kb(*buttons, [("К списку", "accounts:all:0", 67)], home_row()))

    @router.callback_query(F.data.regexp(r"^account:\d+$"))
    async def account_callback(c):
        await card(c, int(c.data.split(":")[1]))

    @router.callback_query(F.data.startswith("account:subscriptions:"))
    async def account_subscriptions(c):
        _, _, account_id, page = c.data.split(":")
        account_id, page = int(account_id), max(0, int(page))
        scope = await channel_scope(s.settings, s.db, c.from_user.id)
        query = (
            select(Channel)
            .join(AccountChannelSubscription)
            .where(
                AccountChannelSubscription.account_id == account_id,
                AccountChannelSubscription.active.is_(True),
                Channel.archived_at.is_(None),
            )
        )
        if scope is not None:
            query = query.where(Channel.id.in_(scope))
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        query.order_by(Channel.id).offset(page * PAGE).limit(PAGE + 1)
                    )
                ).all()
            )
        await show(
            c,
            "Подписки по локальным данным. Сверить их с Telegram можно из карточки канала.",
            kb(
                *[[("@" + row.username, f"channel:{row.id}", 25)] for row in rows[:PAGE]],
                pages(f"account:subscriptions:{account_id}", page, len(rows) > PAGE),
                [("Аккаунт", f"account:{account_id}", 67)],
            ),
        )

    async def proxy_picker(event, account_id, page=0):
        async with s.db.session() as session:
            account = await session.get(TelegramAccount, account_id)
            if not account or account.archived_at:
                raise ValueError("Аккаунт не найден")
            missing_proxy = bool(
                account.last_proxy_id and not await session.get(Proxy, account.last_proxy_id)
            )
            rows = list(
                (
                    await session.scalars(
                        select(Proxy)
                        .where(
                            Proxy.active.is_(True),
                            Proxy.last_check_ok.is_(True),
                            Proxy.last_checked_at
                            >= utcnow() - timedelta(hours=s.settings.proxy_check_max_age_hours),
                        )
                        .order_by(Proxy.id)
                        .offset(max(0, page) * PAGE)
                        .limit(PAGE + 1)
                    )
                ).all()
            )
        await show(
            event,
            f"<b>Прокси · аккаунт №{account.display_number}</b>\n"
            + (
                "Прежний прокси удалён. Выберите новый."
                if missing_proxy
                else f"Сейчас: {account.last_proxy_id or 'автовыбор'}. Выберите проверенный прокси."
            )
            + ("\nНет доступных — сначала добавьте и проверьте." if not rows else ""),
            kb(
                *[
                    [
                        (
                            f"{'✓ ' if r.id == account.last_proxy_id else ''}#{r.id} {r.protocol.value} · {r.host}:{r.port}",
                            f"own:route:choose:{account_id}:{r.id}",
                            28,
                        )
                    ]
                    for r in rows[:PAGE]
                ],
                pages(f"own:route:page:{account_id}", page, len(rows) > PAGE),
                [("Автовыбор", f"own:route:choose:{account_id}:0", 63)],
                [("Проверить прокси", "own:proxy:check", 123)],
                [("Назад", f"account:{account_id}", 67)],
            ),
        )

    @router.callback_query(F.data.startswith("own:route:"))
    async def route(c):
        _, _, action, account_id, value = c.data.split(":")
        account_id, value = int(account_id), int(value)
        if action == "page":
            await proxy_picker(c, account_id, value)
        elif action == "choose":
            await show(
                c,
                f"Назначить прокси #{value}?"
                if value
                else "Включить автовыбор маршрута по настройкам?",
                kb(
                    [("Применить", f"own:route:apply:{account_id}:{value}", 26)],
                    [("Отмена", f"own:route:page:{account_id}:0", 67)],
                ),
            )
        elif action == "apply":
            flow = s.onboarding.active
            if flow and flow.reauth_id == account_id:
                raise ValueError("Сначала завершите добавление этого аккаунта")
            await s.runtime.set_proxy(account_id, value)
            await audit(s.db, c.from_user.id, "account.proxy", account_id, proxy_id=value)
            await card(c, account_id)

    @router.callback_query(F.data.startswith("own:account:"))
    async def mutate(c, state):
        _, _, action, id_ = c.data.split(":")
        account_id = int(id_)
        flow = s.onboarding.active
        if (
            flow
            and flow.reauth_id == account_id
            and action in ("enable", "disable", "delete", "proxy")
        ):
            s.onboarding.cancel(c.from_user.id, flow.token)
            if flow.task:
                await flow.task
        if action in ("enable", "disable"):
            await s.accounts.set_status(
                account_id,
                AccountStatus.ACTIVE if action == "enable" else AccountStatus.DISABLED,
                c.from_user.id,
            )
            await card(c, account_id)
        elif action == "check":
            await show(
                c,
                "Проверка началась. Результат появится отдельным сообщением.",
                kb([("К аккаунту", f"account:{account_id}")]),
            )

            async def check():
                _, message = await s.accounts.check(account_id, c.from_user.id)
                await c.message.answer(
                    escape(message), reply_markup=kb([("Аккаунт", f"account:{account_id}")])
                )

            s.spawn(check(), c.message)
        elif action == "proxy":
            await state.clear()
            await proxy_picker(c, account_id)
        elif action == "label":
            await state.set_state(Input.text)
            await state.update_data(action="account_" + action, account_id=account_id)
            await show(
                c,
                "Введите имя аккаунта.",
                kb(home_row()),
            )
        elif action == "archive":
            await show(
                c,
                "Аккаунт будет остановлен, а его локальная сессия удалена. История отчётов сохранится. Для отзыва доступа на стороне Telegram завершите сеанс в «Устройствах».",
                kb(
                    [("Подтвердить удаление", f"own:account:delete:{account_id}", 33)],
                    [("Отмена", f"account:{account_id}", 67)],
                ),
            )
        elif action == "delete":
            await s.accounts.delete(account_id, c.from_user.id)
            await listing(c, state)
        elif action == "reauth":
            async with s.db.session() as session:
                row = await session.get(TelegramAccount, account_id)
                if not row or row.archived_at:
                    raise ValueError("Аккаунт не найден")
            token = s.onboarding.prepare(
                [ImportItem("Повторный вход", "phone", row.phone)], account_id
            )
            await show(
                c,
                "Текущая работа аккаунта будет остановлена. Выберите способ входа.",
                kb(
                    [("Войти по коду", f"own:import:startcode:{token}", 1, "success")],
                    [("Войти по QR", f"own:import:start:{token}", 26, "success")],
                    [("Отмена", f"own:import:discard:{token}", 67)],
                ),
            )

    @router.callback_query(F.data == "own:accounts:check")
    async def check_all(c):
        if any(task.get_name() == "check-accounts" for task in s.background):
            raise ValueError("Проверка уже выполняется")

        async def run():
            async with s.db.session() as session:
                ids = list(
                    (
                        await session.scalars(
                            select(TelegramAccount.id).where(TelegramAccount.archived_at.is_(None))
                        )
                    ).all()
                )
            ok = 0
            for id_ in ids:
                try:
                    result, _ = await asyncio.wait_for(
                        s.accounts.check(id_, c.from_user.id), s.settings.rpc_timeout_seconds + 15
                    )
                    ok += bool(result)
                except TimeoutError:
                    pass
            await c.message.answer(
                f"Проверка завершена. Успешно: {ok}/{len(ids)}. Занятые аккаунты и аккаунты с ожиданием Telegram пропущены.",
                reply_markup=kb([("Проблемные", "accounts:problems:0")]),
            )

        s.spawn(run(), c.message).set_name("check-accounts")
        await show(
            c,
            "Проверка запущена. Карточки обновляются по мере получения результатов.",
            kb([("Аккаунты", "accounts:all:0")]),
        )

    @router.callback_query(F.data == "own:add")
    async def add_menu(c, state):
        await state.clear()
        await show(
            c,
            "<b>Добавить аккаунты</b>\nФайл, номер и код или QR.\n"
            "Файлы дают доступ к аккаунтам; бот удаляет их из чата перед импортом.",
            kb(
                [("Загрузить файл", "own:add:file", 79)],
                [("Вход по номеру и коду", "own:add:code", 1, "success")],
                [("Вход по QR", "own:add:qr", 3), ("Список номеров", "own:add:phones", 1)],
                [("История", "own:imports:0", 30)],
                home_row(),
            ),
        )

    @router.callback_query(F.data.startswith("own:add:"))
    async def add_choice(c, state):
        choice = c.data.rsplit(":", 1)[1]
        if choice == "qr":
            token = s.onboarding.prepare([ImportItem("QR", "qr", "")])
            await show(
                c,
                "Подтвердите создание новой сессии аккаунта.",
                kb([("Показать QR", f"own:import:start:{token}", 26)], home_row()),
            )
        elif choice in ("code", "phones", "file"):
            await state.set_state(Input.file if choice == "file" else Input.text)
            await state.update_data(action="account_import")
            await show(
                c,
                (
                    "Пришлите .session, ZIP, JSON или TXT.\nБез готовой сессии — код или QR. tdata не поддерживается."
                    if choice == "file"
                    else "Номер или список с новой строки: +79991234567. Затем выберите код или QR."
                )
                + f"\nДо {s.settings.max_import_accounts} аккаунтов / {s.settings.max_upload_bytes // 1_000_000} МБ.",
                kb(home_row()),
            )

    async def preview(message, state, items):
        token = s.onboarding.prepare(items)
        await state.clear()
        sessions = sum(x.kind == "session" for x in items)
        placeholders = sum(x.note in {"empty_session", "missing_session"} for x in items)
        placeholder_text = (
            f"\nБез ключа сессии: {placeholders} — потребуется код или QR." if placeholders else ""
        )
        buttons = (
            [
                [("Добавить по коду", f"own:import:startcode:{token}", 1)],
                [("Добавить по QR", f"own:import:start:{token}", 26)],
            ]
            if sessions < len(items)
            else [[("Добавить", f"own:import:start:{token}", 26)]]
        )
        await message.answer(
            f"<b>Добавление</b> · {len(items)}\n"
            f"Готовые сессии: {sessions} · Нужен вход: {len(items) - sessions}.\n"
            "Уже добавленные аккаунты будут пропущены."
            f"{placeholder_text}\nПодтвердите в течение 5 минут.",
            reply_markup=kb(
                *buttons,
                [("Отмена", f"own:import:discard:{token}", 67)],
            ),
        )

    async def import_file(message, state, data):
        if data.get("action") != "account_import":
            return False
        require_owner(s, message.from_user.id)
        filename, content = await download_secret(message, s.settings)
        try:
            items = await asyncio.to_thread(parse_upload, filename, content, s.settings)
        except ValueError as exc:
            raise ValueError(f"Файл не прошёл проверку: {str(exc)[:700]}") from exc
        except Exception as exc:
            raise ValueError(
                "Файл не прошёл проверку. Проверьте формат, количество записей и целостность сессий. Поддерживаются сессии Telethon; tdata и исполняемые файлы не принимаются."
            ) from exc
        finally:
            content = b""
        await preview(message, state, items)
        return True

    @router.callback_query(F.data.startswith("own:import:"))
    async def import_action(c):
        _, _, action, token = c.data.split(":")
        if action in ("start", "startcode"):
            batch_id = await s.onboarding.start(
                token, c.from_user.id, login_method="code" if action == "startcode" else "qr"
            )
            await show(
                c,
                "Добавление запущено.",
                kb(
                    [("Прогресс", f"import:result:{batch_id}")],
                    [("Остановить", f"import:stop:{token}", 125)],
                ),
            )
        elif action == "discard":
            s.onboarding.cancel(c.from_user.id, token)
            await show(c, "Предпросмотр закрыт.", kb([("Добавить аккаунты", "own:add")]))

    @router.callback_query(F.data.startswith("login:code:"))
    async def login_code(c):
        require_owner(s, c.from_user.id)
        await s.onboarding.code_button(c)

    @router.callback_query(F.data.startswith("import:"))
    async def import_runtime(c):
        require_owner(s, c.from_user.id)
        _, action, id_ = c.data.split(":")
        if action in ("skip", "stop"):
            s.onboarding.cancel(c.from_user.id, id_, skip=action == "skip")
            await c.message.answer(
                "Запрошен пропуск аккаунта." if action == "skip" else "Запрошена остановка импорта."
            )
        elif action == "result":
            async with s.db.session() as session:
                batch = await session.get(ImportBatch, id_)
            if not batch:
                raise ValueError("Импорт не найден")
            rows = batch.results or []
            description = "\n".join(
                f"{r['index']}. №{r.get('number', '—')} · {escape(r['detail'])}" for r in rows[:15]
            )
            await show(
                c,
                f"<b>Импорт {batch.id[:8]}</b> · {escape(batch.status)}\nЗаписей: {len(rows)}/{batch.total}\n{description}"
                + ("\nОстальные записи — в XLSX." if len(rows) > 15 else ""),
                kb(
                    [("Полный результат XLSX", f"import:export:{id_}", 30)],
                    [("Обновить", f"import:result:{id_}", 63)],
                    [("Аккаунты", "accounts:all:0")],
                ),
            )
        elif action == "export":
            async with s.db.session() as session:
                batch = await session.get(ImportBatch, id_)
            if not batch:
                raise ValueError("Импорт не найден")
            rows = [["Строка", "№ аккаунта", "Статус", "Результат"]] + [
                [r["index"], r.get("number", ""), r["status"], r["detail"]] for r in batch.results
            ]
            path = await s.reports._write("import_" + batch.id, [("Добавление аккаунтов", rows)])
            await c.message.answer_document(FSInputFile(path), protect_content=True)
            await audit(s.db, c.from_user.id, "import.exported", id_)

    @router.callback_query(F.data.startswith("own:imports:"))
    async def history(c):
        page = max(0, int(c.data.rsplit(":", 1)[1]))
        async with s.db.session() as session:
            rows = list(
                (
                    await session.scalars(
                        select(ImportBatch)
                        .order_by(ImportBatch.created_at.desc())
                        .offset(page * PAGE)
                        .limit(PAGE + 1)
                    )
                ).all()
            )
        await show(
            c,
            "<b>История добавления аккаунтов</b>",
            kb(
                *[
                    [(f"{date(s, r.created_at)} · {r.total} · {r.status}", f"import:result:{r.id}")]
                    for r in rows[:PAGE]
                ],
                pages("own:imports", page, len(rows) > PAGE),
                home_row(),
            ),
        )

    async def text_input(message, state, data):
        action = data.get("action")
        if action == "account_search":
            await state.update_data(account_search=message.text.strip()[:64])
            await state.set_state(None)
            await listing(message, state)
        elif action == "account_import":
            require_owner(s, message.from_user.id)
            from app.services.accounts import normalize_phone

            items = [
                ImportItem(str(i), "phone", normalize_phone(line))
                for i, line in enumerate(message.text.splitlines(), 1)
                if line.strip()
            ]
            # Phone list is personal data; remove the source before keeping the preview in memory.
            try:
                await message.delete()
            except Exception as exc:
                raise ValueError("Не удалось удалить список номеров; импорт не начат.") from exc
            unique = {item.value: item for item in items}
            await preview(message, state, list(unique.values()))
        elif action in ("account_label", "account_proxy"):
            require_owner(s, message.from_user.id)
            account_id = data["account_id"]
            if action == "account_proxy":
                proxy_id = int(message.text.strip())
                await s.runtime.set_proxy(account_id, proxy_id)
            async with s.db.session() as session:
                row = await session.get(TelegramAccount, account_id)
                if not row or row.archived_at:
                    raise ValueError("Аккаунт не найден")
                if action == "account_label":
                    row.label = message.text.strip()[:255]
            await audit(s.db, message.from_user.id, "account." + action.split("_")[1], account_id)
            await state.clear()
            await card(message, account_id)
        else:
            return False
        return True

    return router, text_input, import_file
